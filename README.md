# Monero-Casino
For the F01–F04 fixes, database migration, required secret/environment settings,
withdrawal recovery worker and tests, see [CRITICAL-FIXES.md](CRITICAL-FIXES.md).
For the F07–F10 deposit fixes and second migration, see [DEPOSIT-SAFETY.md](DEPOSIT-SAFETY.md).
Remaining audited issues still block launch; use disposable stagenet funds for QA.

Online casino which is provably fair with a transparent random number generating system for the results using the hash from blocks mined on the Monero blockchain. Built in python and hosted on a flask web server with a MySQL database the application has three betting options. 
1. A bet on the number being odd or even.
2.  A bet on the exact outcome.
3.  A bet on it being above or below 50.
![thumbnail_processed-E5AA6C42-7E6A-4B79-AD1D-C33D5981127C](https://github.com/user-attachments/assets/fadadba4-5135-4521-97bf-1ad2dfb69c44)



# How is the winning number found?
By using the proof of work derived from the hashrate of Moneros blockchain I use python scripts and cron jobs to check the hashes of every fifth block found the final 2 digits of this hash are the winning numbers of that round. For example the if the hash ends gt564hj6s702 then the winning number is 02. If there is a letter between them then that letter is stripped until there is a two digit integer.

# How do the transactions and wallets work?
Users deposit into subaddresses of the application's account-0 Monero wallet. The deposit scanner observes incoming transfers and credits the database after confirmation and unlock checks. It never sweeps funds; withdrawals spend from that same wallet through durable reservations.

# How do bets work? 
All bets, wins and losses are tracked in the database. Bets are not actually conducted on the monero blockchain they are performed by a python function. This prevents the clogging of the blockchain with small transactions and the unnecessary RPC requests. When the user wants to withdraw monero their balance is sent from the house wallet to their own private address, this is then updated in the database.
![thumbnail_processed-64BE1994-069D-499C-BF56-306139401551](https://github.com/user-attachments/assets/8bf5d7e2-3e67-4eb1-aefa-dde8b1f9e0e2)
