from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
import mysql.connector
import time
import datetime
import logging
import hashlib
import functools
import requests
import qrcode
import io
import os
import sync_blocks
from decimal import Decimal


app = Flask(__name__)
app.secret_key = "your_super_secret_key"


logging.basicConfig(level=logging.DEBUG)

db = mysql.connector.connect(
    host="192.168.1.180",         # your server's IP address
    user="casino_admin",
    password="",
    database="monero_casino"
)

def get_db_connection():
    return mysql.connector.connect(
        host="192.168.1.180",
        user="casino_admin",
        password="",
        database="monero_casino"
    )
    
cursor = db.cursor()
print("✅ Database connection successful!")

app.config["DEBUG"] = True
app.config["PROPAGATE_EXCEPTIONS"] = True

REMOTE_MONERO_NODE = "http://192.168.1.180:18089/json_rpc"

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
    payload = {
        "jsonrpc": "2.0",
        "id": "0",
        "method": "get_block_count",
        "params": {}
    }

    try:
        response = requests.post(REMOTE_MONERO_NODE, json=payload, timeout=10)
        print("✅ Monero RPC status:", response.status_code)
        print("✅ Monero RPC body:", response.text)
        response_json = response.json()
        return response_json["result"]["count"]
    except Exception as e:
        print("❌ Error fetching block height:", e)
        return "Unavailable"
    
def get_next_betting_block(current_block):
    """Finds the next valid betting block (must end in 0 or 5 and cannot be within 1 block of it)."""

    # Find the next multiple of 5 that is greater than the current block
    next_block = (current_block // 5 + 1) * 5

    # Ensure betting is not within 1 block of the next multiple of 5
    if next_block - 1 == current_block:
        next_block += 5  # Skip to the next valid betting block

    return next_block



def send_monero_transaction(address, amount):
    url = "http://192.168.1.180:18083/json_rpc"  # Local Monero daemon RPC URL
    headers = {"Content-Type": "application/json"}

    # Prepare the transaction parameters
    data = {
        "jsonrpc": "2.0",
        "method": "transfer",
        "params": {
            "destinations": [{"amount": int(amount * 1e12), "address": address}],  # Convert XMR to atomic units (1e12)
            "mixin": 10,  # Number of mixins (adjust for anonymity)
            "priority": 0,  # Adjust as needed
        },
        "id": 1
    }

    try:
        # Send the request to the Monero node
        response = requests.post(url, json=data, headers=headers)
        response_data = response.json()

        if response_data.get('result'):
            # Transaction successful, return tx_hash
            tx_hash = response_data['result']['tx_hash']
            return True, tx_hash
        else:
            # Handle failure case
            return False, response_data.get('error', {}).get('message', 'Unknown error')
    except Exception as e:
        return False, str(e)

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
            response = requests.post("http://192.168.1.180:18083/json_rpc", json=payload)
            response_json = response.json()

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
    latest_block = get_latest_block_height()
    next_bet_block = get_next_betting_block(latest_block)

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
    print("✅ Received bet submission.")

    user_id = session.get("user_id")
    latest_block = get_latest_block_height()
    next_bet_block = get_next_betting_block(latest_block)

    bet_amount_str = request.form.get("bet_amount")
    game_type = request.form.get("game_type")
    chosen_number = request.form.get("chosen_number")

    print(f"User ID: {user_id}, Bet Amount: {bet_amount_str}, Game Type: {game_type}, Next Bet Block: {next_bet_block}")

    if not bet_amount_str:
        flash("Invalid bet amount!", "error")
        print("❌ Invalid bet amount!")
        return redirect(url_for("bet"))

    try:
        bet_amount = Decimal(bet_amount_str)
    except:
        flash("Invalid bet amount!", "error")
        return redirect(url_for("bet"))

    if bet_amount <= 0:
        flash("Invalid bet amount!", "error")
        return redirect(url_for("bet"))

    cursor.execute("SELECT virtual_balance FROM wallets WHERE user_id = %s", (user_id,))
    wallet = cursor.fetchone()
    if not wallet or Decimal(wallet[0]) < bet_amount:
        flash("Insufficient virtual balance.", "error")
        return redirect(url_for("bet"))

    cursor.execute("SELECT id FROM blocks WHERE block_height = %s", (next_bet_block,))
    block = cursor.fetchone()

    if not block:
        cursor.execute(
            "INSERT INTO blocks (block_height, block_hash, last_two_digits) VALUES (%s, %s, %s)",
            (next_bet_block, "pending", next_bet_block % 100),
        )
        db.commit()
        block_id = cursor.lastrowid
    else:
        block_id = block[0]

    if game_type == "digits":
        if not chosen_number or not chosen_number.isdigit() or not (0 <= int(chosen_number) <= 99):
            flash("Invalid number selection!", "error")
            print("❌ Invalid chosen number!")
            return redirect(url_for("bet"))

        cursor.execute(
            "INSERT INTO bets (user_id, block_id, bet_amount, chosen_number, bet_value, game_type, bet_status) VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
            (user_id, block_id, str(bet_amount), chosen_number, None, "digits")
        )

    elif game_type == "odd_even":
        chosen_option = request.form.get("chosen_option")
        if chosen_option not in ["odd", "even"]:
            flash("Invalid selection!", "error")
            return redirect(url_for("bet"))

        cursor.execute(
            "INSERT INTO bets (user_id, block_id, bet_amount, chosen_number, bet_value, game_type, bet_status) VALUES (%s, %s, %s, NULL, %s, %s, 'pending')",
            (user_id, block_id, str(bet_amount), chosen_option, "odd_even")
        )

    elif game_type == "high_low":
        chosen_high_low = request.form.get("chosen_high_low")
        if chosen_high_low not in ["high", "low"]:
            flash("Invalid selection!", "error")
            return redirect(url_for("bet"))

        cursor.execute(
            "INSERT INTO bets (user_id, block_id, bet_amount, chosen_number, bet_value, game_type, bet_status) VALUES (%s, %s, %s, NULL, %s, %s, 'pending')",
            (user_id, block_id, str(bet_amount), chosen_high_low, "high_low")
        )

    else:
        flash("Unknown game type.", "error")
        return redirect(url_for("bet"))

    cursor.execute(
        "UPDATE wallets SET virtual_balance = virtual_balance - %s WHERE user_id = %s",
        (str(bet_amount), user_id)
    )

    db.commit()
    flash("✅ Bet placed successfully!", "success")
    print("✅ Bet successfully inserted into database.")
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

@app.route("/withdraw", methods=["GET", "POST"])
@login_required
def withdraw():
    user_id = session.get("user_id")

    # Fetch the user's virtual balance
    cursor.execute("SELECT virtual_balance FROM wallets WHERE user_id = %s", (user_id,))
    wallet = cursor.fetchone()
    virtual_balance = wallet[0] if wallet else 0.0
    print(f"Virtual Balance: {virtual_balance}")

    if request.method == "POST":
        address = request.form.get("withdraw_address")
        amount = request.form.get("withdraw_amount")

        print(f"Withdraw request -> Address: {address}, Amount: {amount}")

        if not address or not amount:
            flash("Invalid withdrawal details.", "error")
            return redirect(url_for("withdraw"))

        try:
            amount = float(amount)
        except ValueError:
            flash("Invalid amount entered.", "error")
            return redirect(url_for("withdraw"))

        if amount < 0.05:
            flash("Minimum withdrawal is 0.05 XMR.", "error")
            return redirect(url_for("withdraw"))

        if virtual_balance < amount:
            flash("Insufficient balance.", "error")
            return redirect(url_for("withdraw"))

        if len(address) != 95:
            flash("Invalid Monero address.", "error")
            return redirect(url_for("withdraw"))

        # Check unlocked balance in house wallet
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": "get_balance",
            "params": {"account_index": 0}
        }

        try:
            response = requests.post("http://192.168.1.180:18083/json_rpc", json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()["result"]
            unlocked_balance = float(result["unlocked_balance"]) / 1e12
            print(f"House wallet unlocked balance: {unlocked_balance} XMR")

            if unlocked_balance < amount:
                flash("Withdrawal failed: House wallet does not have enough unlocked funds.", "error")
                return redirect(url_for("withdraw"))
        except Exception as e:
            print("❌ Error checking unlocked balance:", e)
            flash("Could not confirm unlocked funds. Try again later.", "error")
            return redirect(url_for("withdraw"))

        # Perform the Monero transfer
        send_payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": "transfer",
            "params": {
                "account_index": 0,
                "destinations": [{"address": address, "amount": int(amount * 1e12)}],
                "priority": 1
            }
        }

        try:
            response = requests.post("http://192.168.1.180:18083/json_rpc", json=send_payload, timeout=15)
            response.raise_for_status()
            result = response.json()["result"]
            tx_hash = result["tx_hash"]

            # Record transaction
            cursor.execute("""
                INSERT INTO transactions (user_id, tx_hash, amount, tx_type)
                VALUES (%s, %s, %s, 'withdrawal')
            """, (user_id, tx_hash, amount))

            # Deduct from user's virtual balance
            cursor.execute("""
                UPDATE wallets SET virtual_balance = virtual_balance - %s
                WHERE user_id = %s
            """, (amount, user_id))

            # Deduct from admin_wallets (house)
            cursor.execute("""
                UPDATE admin_wallets SET balance = balance - %s
                WHERE label = 'house'
            """, (amount,))

            db.commit()

            flash(f"✅ Withdrawal successful. TX: {tx_hash}", "success")
        except Exception as e:
            print("❌ Withdrawal failed:", e)
            flash(f"Withdrawal failed: {e}", "error")

        return redirect(url_for("withdraw"))

    return render_template("withdraw.html", balance=virtual_balance)


@app.route("/resolve_test/<int:block_height>")
def resolve_test(block_height):
    from bet_resolver import get_block_hash, extract_last_two_digits

    try:
        block_hash = get_block_hash(block_height)
        if block_hash is None:
            return f"❌ No block hash returned for block {block_height}"
        
        digits = extract_last_two_digits(block_hash)
        return f"Block {block_height} hash: {block_hash}<br>Last two digits: {digits}"
    except Exception as e:
        return f"❌ Error: {e}"
    
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
    # Check if current user is an admin
    cursor.execute("SELECT is_admin FROM users WHERE id = %s", (session["user_id"],))
    is_admin = cursor.fetchone()
    if not is_admin or is_admin[0] != 1:
        flash("Unauthorized access.", "error")
        return redirect(url_for("home"))

    # Fetch house wallet info
    cursor.execute("SELECT id, balance FROM admin_wallets WHERE label = 'house'")
    house_wallet = cursor.fetchone()
    if not house_wallet:
        flash("House wallet not found.", "error")
        return redirect(url_for("admin_withdrawal"))

    admin_wallet_id, house_balance = house_wallet
    house_balance = Decimal(house_balance)

    if request.method == "POST":
        address = request.form.get("withdraw_address")
        amount = request.form.get("withdraw_amount")

        print(f"Admin Withdraw → Address: {address}, Amount: {amount}")

        try:
            amount = Decimal(amount)
        except ValueError:
            flash("Invalid amount.", "error")
            return redirect(url_for("admin_withdrawal"))

        if house_balance < amount:
            flash("Insufficient funds in house wallet.", "error")
            return redirect(url_for("admin_withdrawal"))

        if len(address) != 95:
            flash("Invalid Monero address.", "error")
            return redirect(url_for("admin_withdrawal"))

        # ✅ Check Monero unlocked balance
        try:
            response = requests.post("http://192.168.1.180:18083/json_rpc", json={
                "jsonrpc": "2.0",
                "id": "0",
                "method": "get_balance",
                "params": {"account_index": 0}
            }, timeout=10)

            result = response.json()
            if "result" not in result:
                flash("Unexpected response from Monero RPC.", "error")
                return redirect(url_for("admin_withdrawal"))

            unlocked_balance = Decimal(result["result"]["unlocked_balance"]) / Decimal(1e12)
            if unlocked_balance < amount:
                flash("House wallet does not have enough unlocked funds.", "error")
                return redirect(url_for("admin_withdrawal"))

        except Exception as e:
            flash(f"RPC error: {e}", "error")
            return redirect(url_for("admin_withdrawal"))

        # ✅ Perform the transfer
        try:
            response = requests.post("http://192.168.1.180:18083/json_rpc", json={
                "jsonrpc": "2.0",
                "id": "0",
                "method": "transfer",
                "params": {
                    "account_index": 0,
                    "destinations": [{
                        "address": address,
                        "amount": int((amount * Decimal(1e12)).to_integral_value())
                    }],
                    "priority": 1
                }
            }, timeout=10)

            tx_hash = response.json()["result"]["tx_hash"]

            # ✅ Log in admin_transactions
            cursor.execute("""
                INSERT INTO admin_transactions (admin_wallet_id, tx_hash, amount, tx_type)
                VALUES (%s, %s, %s, 'withdrawal')
            """, (admin_wallet_id, tx_hash, amount))

            # ✅ Deduct from house balance
            cursor.execute("""
                UPDATE admin_wallets SET balance = balance - %s WHERE id = %s
            """, (amount, admin_wallet_id))

            db.commit()
            flash(f"✅ Admin withdrawal successful. TX: {tx_hash}", "success")

        except Exception as e:
            flash(f"Withdrawal failed: {e}", "error")

        return redirect(url_for("admin_withdrawal"))

    return render_template("admin_withdrawal.html", balance=f"{house_balance:.12f}")


def get_recent_winning_numbers(limit=10):
    cursor = db.cursor()
    cursor.execute("""
        SELECT block_height, last_two_digits
        FROM blocks
        WHERE last_two_digits IS NOT NULL
        ORDER BY block_height DESC
        LIMIT %s
    """, (limit,))
    return cursor.fetchall()





if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)

##