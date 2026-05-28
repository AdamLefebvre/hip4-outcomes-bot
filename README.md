# HIP-4 Outcomes Farming Bot

A volume-farming bot for Hyperliquid's HIP-4 binary outcome markets, designed to generate trading volume for potential Airdrop 2 eligibility.

## Strategy

- Places **limit buy** orders at mid-price (limit orders = zero fees)
- **Take-profit** at +5%
- **DCA** at -10% (up to 2 additional legs)
- **Hard stop** at -35%
- Auto-detects the active daily market via `outcomeMeta`
- Loops indefinitely after each close, with a configurable pullback cooldown before re-entry

## Requirements

- Python 3.10+
- A Hyperliquid wallet with USDC in the **spot** balance (not perp margin)

## Setup

```bash
# Clone the repo
git clone https://github.com/AdamLefebvre/hip4-outcomes-bot.git
cd hip4-outcomes-bot

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install hyperliquid-python-sdk eth-account python-dotenv requests

# Configure your private key
cp .env.example .env
# Edit .env and set PRIVATE_KEY=0x...
```

## Usage

```bash
# Run the bot
python hl_outcomes_bot.py

# List active Outcome markets
python hl_outcomes_bot.py --list-markets

# Skip pullback cooldown between rounds
python hl_outcomes_bot.py --no-pullback

# Debug market data
python hl_outcomes_bot.py --debug
```

## Configuration

Edit the constants at the top of `hl_outcomes_bot.py`:

| Parameter | Default | Description |
|---|---|---|
| `SIDE` | `"NO"` | Which side to trade: `"YES"` or `"NO"` |
| `BASE_USDH` | `10.0` | USDC per leg |
| `TAKE_PROFIT_PCT` | `0.05` | Take-profit threshold (+5%) |
| `DCA_TRIGGER_PCT` | `-0.10` | DCA trigger (-10%) |
| `HARD_STOP_PCT` | `-0.35` | Hard stop (-35%) |
| `MAX_DCA_LEGS` | `2` | Maximum DCA entries |
| `ROUNDS` | `0` | Number of rounds (0 = infinite) |

## Security

- Never commit your `.env` file — it is excluded by `.gitignore`
- The bot only places spot orders on Hyperliquid; it does not touch perp margin
