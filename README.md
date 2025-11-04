# Monero-Casino
Online casino which is provably fair with a transparent random number generating system for the results using the hash from blocks on the Monero blockchain. Built in python and hosted on a flask web server with a MySQL database the application has three betting options. 
1. A bet on the number being odd or even.
2.  A bet on the exact outcome.
3.  A bet on it being above or below 50.
![thumbnail_processed-E5AA6C42-7E6A-4B79-AD1D-C33D5981127C](https://github.com/user-attachments/assets/fadadba4-5135-4521-97bf-1ad2dfb69c44)



# How is the winning number found?
By using the proof of work derived from the hashrate of Moneros blockchain I use python scripts and cron jobs to check the hashes of every fifth block found the final 2 digits of this hash are the winning numbers of that round. For example the if the hash ends gt564hj6s702 then the winning number is 02. If there is a letter between them then that letter is stripped until there is a two digit integer.

# How do the transactions and wallets work?
Using a full monero node (could use pruned) you create a dedicated RPC for the application to interact directly with the blockchain. Using the CLI when a user creates an account a wallet is automatically generated for them. When they deposit money however, they are deposting it to the house balance and the database keeps track of their balance in paper. All bets, wins and losses are tracked in the database. Bets are not actually conducted on the monero blockchain they are performed by a python function. This prevents the clogging of the blockchain with small transactions. When the user wants to withdraw monero their balance is sent from the house wallet to their own private address, this is then updated in the database. ![thumbnail_processed-64BE1994-069D-499C-BF56-306139401551]
![thumbnail_processed-64BE1994-069D-499C-BF56-306139401551](https://github.com/user-attachments/assets/8bf5d7e2-3e67-4eb1-aefa-dde8b1f9e0e2)
