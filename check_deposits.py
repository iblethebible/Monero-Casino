"""One-shot deposit observation and credit job. Never sweeps or spends funds."""
from deposits import scan_deposits


def check_and_update_balances():
    return scan_deposits()


if __name__ == '__main__':
    print(check_and_update_balances())
