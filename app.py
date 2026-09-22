from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
import mysql.connector
import logging
import hashlib
import functools
import requests
import qrcode
import io
from decimal import Decimal


from uuid import uuid4
from database import db, cursor, get_db_connection, close_db
from security_config import configure_security
from rounds import get_chain_tip, next_betting_height
from betting import place_bet as reserve_bet
from withdrawals import reserve_withdrawal, process_withdrawal
import wallet_rpc

app = Flask(__name__)
configure_security(app)
app.teardown_appcontext(close_db)
app.config.update(DEBUG=False, PROPAGATE_EXCEPTIONS=False)
logging.basicConfig(level=logging.INFO)

@app.after_request
def add_header(response):
    response.cache_control.no_cache = True
    response.cache_control.no_store = True
    response.cache_control.must_revalidate = True
    response.headers['Pragma'] = 'no-cache'  # Older HTTP headers for caching
    response.headers['Expires'] = '0'  # Prevent caching for HTTP/1.0 browsers
    return response

@app.context_processor
def inject_admin_flag():
    user_id = session.get("user_id")
    if not user_id:
        return {'is_admin': False}

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()

    return {'is_admin': row and row[0] == 1}


def get_latest_block_height():
    return get_chain_tip()


def get_next_betting_block(current_block):
    return next_betting_height(current_block)


def login_required(func):
    """Decorator to ensure user is logged in"""
    @functools.wraps(func)  # ✅ This preserves the function name
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper

# Redirect root to login page
@app.route("/")
def index():
    return redirect(url_for("login"))

@app.route('/home')
@login_required
def home():
    try:
        print("✅ Accessing /home route")
        latest_block = get_latest_block_height()
        next_bet_block = get_next_betting_block(latest_block)

        return render_template("home.html",
                           latest_block=latest_block,
                           next_bet_block=next_bet_block,
                           prize_pool=10)
    except Exception as e:
        print("❌ Error in /home route:", e)
        return render_template("home.html",
                               latest_block="Unavailable",
                               next_bet_block="Unavailable",
                               prize_pool=10)




@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        if not username or not password:
            flash("Please provide both username and password.", "error")
            return render_template("register.html")

        password_hash = hashlib.sha256(password.encode()).hexdigest()

        try:
            cursor.execute(
                "INSERT INTO users (username, password_hash, wallet_id) VALUES (%s, %s, %s)",
                (username, password_hash, None)
            )
            db.commit()
        except mysql.connector.IntegrityError as e:
            db.rollback()
            flash(f"Username already exists or database error: {str(e)}", "error")
            return render_template("register.html")

        user_id = cursor.lastrowid
        print("✅ User created with ID:", user_id)

        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": "create_address",
            "params": {
                "account_index": 0,
                "label": username
            }
        }

        try:
            response_json = {"result": wallet_rpc.call("create_address", payload["params"])}

            if "result" not in response_json or "address" not in response_json["result"]:
                raise Exception(f"Monero RPC returned invalid response: {response_json}")

            monero_address = response_json["result"]["address"]
            print("✅ Generated address:", monero_address)

            cursor.execute(
                "INSERT INTO wallets (user_id, address, balance, virtual_balance) VALUES (%s, %s, %s, %s)",
                (user_id, monero_address, 0.0, 0.0)
            )
            db.commit()
            wallet_id = cursor.lastrowid
            print("✅ Wallet inserted with ID:", wallet_id)

            cursor.execute(
                "UPDATE users SET wallet_id = %s WHERE id = %s",
                (wallet_id, user_id)
            )
            db.commit()

            session.clear()
            session["user_id"] = user_id
            session["username"] = username


            flash("Registration successful! You are logged in.", "success")
            return redirect(url_for("profile"))

        except Exception as e:
            db.rollback()
            print("❌ Error during Monero RPC or DB insert:", e)
            flash(f"Registration failed: {e}", "error")
            return render_template("register.html")

    return render_template("register.html")




@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        # Hash the password
        password_hash = hashlib.sha256(password.encode()).hexdigest()

        cursor.execute("SELECT id, username, is_admin FROM users WHERE username = %s AND password_hash = %s",
                       (username, password_hash))
        user = cursor.fetchone()

        if user:
            user_id, username, is_admin = user
            session.clear()
            session["user_id"] = user_id
            session["username"] = username
            session["is_admin"] = is_admin  # ✅ Store for quick access if needed

            if is_admin:
                return redirect(url_for("admin_dashboard"))
            else:
                return redirect(url_for("home"))
        else:
            return "❌ Invalid username or password!", 400

    return render_template("login.html")


@app.route("/profile")
@login_required
def profile():
    user_id = session.get("user_id")

    # Fetch user details (username, wallet_id)
    cursor.execute("SELECT username, wallet_id FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()

    if not user:
        return "User not found", 404

    username, wallet_id = user


    # Fetch the deposit address and virtual balance
    cursor.execute("SELECT address, virtual_balance FROM wallets WHERE id = %s", (wallet_id,))
    wallet = cursor.fetchone()


    if wallet:
        print(f"Fetched wallet: {wallet}")
        deposit_address, virtual_balance = wallet[0], float(wallet[1]) if wallet[1] else 0.0
    else:
        deposit_address, virtual_balance = "No Address Found", 0.0

        session["balance"] = virtual_balance  # Update balance in the session


    # Generate a QR code dynamically (without saving to disk)
    qr_code_filename = f"{user_id}_qr.png"

    # Fetch full betting history with block height
    cursor.execute(
        """
        SELECT blocks.block_height, bets.bet_amount, bets.chosen_number, bets.bet_value, bets.game_type, bets.bet_status, bets.created_at
        FROM bets
        JOIN blocks ON bets.block_id = blocks.id
        WHERE bets.user_id = %s
        ORDER BY bets.created_at DESC
        LIMIT 10
        """,
        (user_id,),
    )
    betting_history = cursor.fetchall()

    # Convert results into a list of dictionaries
    betting_history = [
        {
            "block_height": bet[0],
            "amount": bet[1],
            "chosen_number": bet[2],
            "bet_value": bet[3],
            "game_type": bet[4],
            "result": bet[5],
            "timestamp": bet[6]
        }
        for bet in betting_history
    ]

    return render_template(
        "profile.html",
        username=username,
        deposit_address=deposit_address,
        qr_code_filename=qr_code_filename,
        balance=virtual_balance,
        betting_history=betting_history
    )


@app.route("/qr/<address>")
def generate_qr_code(address):
    """Generates a QR code dynamically for the deposit address without saving to disk."""
    img = qrcode.make(address)
    img_io = io.BytesIO()
    img.save(img_io, format='PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype="image/png")

@app.route("/bet", methods=["GET", "POST"])
@login_required
def bet():
    user_id = session.get("user_id")
    try:
        latest_block = get_latest_block_height()
        next_bet_block = get_next_betting_block(latest_block)
    except Exception:
        flash("Betting is temporarily unavailable while the node is unavailable or synchronizing.", "error")
        return render_template("bet.html", latest_block="Unavailable", next_bet_block="Unavailable",
                               active_digits_bets=[], active_odd_even_bets=[], active_high_low_bets=[]), 503

    # Get actual block_id from blocks table
    cursor.execute("SELECT id FROM blocks WHERE block_height = %s", (next_bet_block,))
    block_row = cursor.fetchone()
    if block_row:
        block_id = block_row[0]
    else:
        block_id = None  # No bets will match if the block isn't even in DB

    if block_id:
        cursor.execute("SELECT bet_amount, chosen_number FROM bets WHERE user_id = %s AND block_id = %s AND game_type = 'digits'", (user_id, block_id))
        active_digits_bets = cursor.fetchall()

        cursor.execute("SELECT bet_amount, bet_value FROM bets WHERE user_id = %s AND block_id = %s AND game_type = 'odd_even'", (user_id, block_id))
        active_odd_even_bets = cursor.fetchall()

        cursor.execute("SELECT bet_amount, bet_value FROM bets WHERE user_id = %s AND block_id = %s AND game_type = 'high_low'", (user_id, block_id))
        active_high_low_bets = cursor.fetchall()
    else:
        # No block in DB yet — no bets to show
        active_digits_bets = []
        active_odd_even_bets = []
        active_high_low_bets = []

    return render_template(
        "bet.html",
        latest_block=latest_block,
        next_bet_block=next_bet_block,
        active_digits_bets=active_digits_bets,
        active_odd_even_bets=active_odd_even_bets,
        active_high_low_bets=active_high_low_bets
    )



@app.route("/place_bet", methods=["POST"])
@login_required
def place_bet():
    game_type = request.form.get("game_type")
    field = {"digits": "chosen_number", "odd_even": "chosen_option", "high_low": "chosen_high_low"}.get(game_type)
    try:
        reserve_bet(session["user_id"], request.form.get("bet_amount"), game_type,
                    request.form.get(field) if field else None)
        flash("Bet placed successfully.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        app.logger.exception("Bet placement failed")
        flash("Bet could not be confirmed. Check your history before trying again.", "error")
    return redirect(url_for("bet"))


@app.route("/convert", methods=["GET", "POST"])
def convert():
    if request.method == "POST":
        fiat_amount = request.form.get("fiat_amount")
        currency = request.form.get("currency")  # USD, GBP, EUR, etc.

        # Fetch latest XMR price
        try:
            response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=monero&vs_currencies=" + currency.lower())
            xmr_price = response.json()["monero"][currency.lower()]
            xmr_equivalent = float(fiat_amount) / xmr_price
            return render_template("convert.html", fiat_amount=fiat_amount, currency=currency, xmr_equivalent=xmr_equivalent)
        except Exception as e:
            return render_template("convert.html", error="Failed to fetch exchange rate")

    # If it's a GET request, just render the empty form
    return render_template("convert.html")



@app.route('/stats')
@login_required
def stats():
    total_won = 0  # Optional: calculate from DB
    recent_winning_numbers = get_recent_winning_numbers()
    return render_template('stats.html', total_won=total_won, recent_winning_numbers=recent_winning_numbers)


@app.route('/jackpot')
@login_required
def jackpot():
    return render_template('jackpot.html')

@app.route('/faq')
def faq():
    return render_template('faq.html')

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

def _withdrawal_page(kind):
    user_id = session["user_id"]
    endpoint = "admin_withdrawal" if kind == "admin" else "withdraw"
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    try:
        cur.execute("SELECT is_admin FROM users WHERE id=%s", (user_id,))
        actor = cur.fetchone()
        if not actor or (kind == "admin" and actor["is_admin"] != 1):
            return "Access denied", 403
    finally:
        cur.close()
        conn.close()
    if request.method == "POST":
        try:
            row = reserve_withdrawal(user_id, kind, request.form.get("withdraw_address"),
                                     request.form.get("withdraw_amount"), request.form.get("request_id"))
            row = process_withdrawal(row["id"])
            if row and row["state"] == "submitted":
                flash(f"Withdrawal submitted. TX: {row['tx_hash']}", "success")
            elif row and row["state"] == "failed":
                flash("The wallet rejected this withdrawal. Your reservation was released.", "error")
            else:
                flash("Withdrawal pending. Funds remain reserved; use Retry on this request or wait for processing.", "info")
        except ValueError as exc:
            flash(str(exc), "error")
        except Exception:
            app.logger.exception("Withdrawal request requires recovery")
            flash("Could not confirm withdrawal status. Check the request history; do not submit a replacement.", "error")
        return redirect(url_for(endpoint))
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    try:
        if kind == "admin":
            cur.execute("SELECT balance AS available FROM admin_wallets WHERE label='house'")
        else:
            cur.execute("""SELECT w.virtual_balance AS available FROM wallets w
                           JOIN users u ON u.wallet_id=w.id WHERE u.id=%s AND w.user_id=u.id""", (user_id,))
        wallet = cur.fetchone()
        cur.execute("""SELECT request_id,address,amount,state,tx_hash,created_at FROM withdrawals
                       WHERE user_id=%s AND kind=%s ORDER BY id DESC LIMIT 20""", (user_id, kind))
        history = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return render_template("admin_withdrawal.html" if kind == "admin" else "withdraw.html",
                           balance=wallet["available"] if wallet else Decimal("0"),
                           request_id=str(uuid4()), withdrawals=history)


@app.route("/withdraw", methods=["GET", "POST"])
@login_required
def withdraw():
    return _withdrawal_page("user")


@app.route("/admin")
@login_required
def admin_dashboard():
    user_id = session.get("user_id")

    conn = get_db_connection()
    cur = conn.cursor()

    # Make sure this user is actually an admin
    cur.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
    result = cur.fetchone()
    if not result or result[0] != 1:
        conn.close()
        return "Access denied", 403

    cursor.execute("SELECT balance FROM admin_wallets WHERE label = 'house'")
    result = cursor.fetchone()
    house_balance = Decimal(result[0]) if result else Decimal('0.0')  # Convert to Decimal
    # Total users
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    # Total XMR wagered
    cur.execute("SELECT SUM(bet_amount) FROM bets")
    total_wagered = cur.fetchone()[0] or Decimal('0.0')  # Convert to Decimal
    # Total payouts
    cur.execute("SELECT SUM(payout) FROM bets WHERE bet_status = 'won'")
    total_payout = cur.fetchone()[0] or Decimal('0.0')  # Convert to Decimal
    # Recent bets (latest 10)
    cur.execute("""
        SELECT b.user_id, u.username, b.game_type, b.bet_amount, b.chosen_number, b.bet_value, b.bet_status, bl.block_height
        FROM bets b
        JOIN users u ON b.user_id = u.id
        JOIN blocks bl ON b.block_id = bl.id
        ORDER BY b.created_at DESC
        LIMIT 10
    """)
    recent_bets = cur.fetchall()

    conn.close()

    return render_template("admin.html",
        house_balance=house_balance,
        total_users=total_users,
        total_wagered=total_wagered,
        total_payout=total_payout,
        recent_bets=recent_bets
    )

@app.route("/admin_withdrawal", methods=["GET", "POST"])
@login_required
def admin_withdrawal():
    return _withdrawal_page("admin")


def get_recent_winning_numbers(limit=10):
    cursor = db.cursor()
    cursor.execute("""
        SELECT block_height, last_two_digits
        FROM blocks
        WHERE last_two_digits IS NOT NULL AND resolved_at IS NOT NULL
        ORDER BY block_height DESC
        LIMIT %s
    """, (limit,))
    return cursor.fetchall()





if __name__ == '__main__':
    app.run(host="127.0.0.1", port=5000, debug=False)

##
