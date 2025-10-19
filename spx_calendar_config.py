#!/usr/bin/env python3
"""
Configuration Template for SPX Double Calendar Spread Trading System

Copy this file and modify the values as needed.
You can also set these as environment variables.
"""

import os
from datetime import time as dt_time

class CalendarConfigTemplate:
    """Configuration template - modify these values"""
    
    # ===============================================
    # IBKR CONNECTION SETTINGS
    # ===============================================
    IB_HOST = "127.0.0.1"
    IB_PORT = 7496  # TWS live: 7496, TWS paper: 7497
    IB_CLIENT_ID = 2
    
    # ===============================================
    # TRADING PARAMETERS
    # ===============================================
    POSITION_SIZE = 4  # contracts per leg
    MAX_CONCURRENT_POSITIONS = 7
    TARGET_DELTA = 0.20
    DELTA_TOLERANCE = 0.05  # acceptable range around target delta
    
    # ===============================================
    # ORDER MANAGEMENT
    # ===============================================
    INITIAL_PRICE_TYPE = "MID"  # start with mid price
    PRICE_INCREMENT = 0.05  # increase bid by this amount
    MAX_PRICE_ATTEMPTS = 5  # total attempts before giving up
    FILL_WAIT_TIME = 60  # DEPRECATED - use ENTRY_FILL_TIMEOUT and EXIT_FILL_TIMEOUT
    
    # Entry order timeouts (can be patient - not filling isn't catastrophic)
    ENTRY_FILL_TIMEOUT = 15  # seconds to wait before adjusting entry price
    ENTRY_MAX_ATTEMPTS = 5  # max attempts for entry orders
    
    # Exit order timeouts (MUST be aggressive - we HAVE to get out!)
    EXIT_FILL_TIMEOUT = 8  # seconds to wait before adjusting exit price (aggressive!)
    EXIT_MAX_ATTEMPTS = 8  # more attempts for exits - we MUST fill
    
    # ===============================================
    # EXIT MANAGEMENT
    # ===============================================
    PROFIT_TARGET_PCT = 0.50  # 50% profit target
    EXIT_DAY = 14  # exit on 14th day after entry
    EXIT_TIME = dt_time(15, 0)  # 3:00 PM ET
    
    # ===============================================
    # SCHEDULE
    # ===============================================
    ENTRY_TIME = dt_time(9, 44, 50)  # 9:44:50 AM ET
    TIMEZONE = "America/New_York"
    
    # ===============================================
    # DATABASE
    # ===============================================
    DB_PATH = "spx_calendar_trades.db"
    
    # ===============================================
    # SMS NOTIFICATIONS (EMAIL-TO-SMS - FREE!)
    # ===============================================
    # Gmail SMTP settings for email-to-SMS gateway
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SENDER_EMAIL = os.getenv('SENDER_EMAIL', 'hall.jamie@gmail.com')
    SENDER_APP_PASSWORD = os.getenv('SENDER_APP_PASSWORD', '')  # Gmail app password
    NOTIFY_TO_NUMBER = "8133344846"  # Your phone number (no +1)
    SMS_GATEWAY = "@vtext.com"  # Xfinity/Verizon gateway


def load_config_from_env():
    """Load configuration from environment variables"""
    config = CalendarConfigTemplate()
    
    # Override with environment variables if they exist
    config.IB_HOST = os.getenv('IB_HOST', config.IB_HOST)
    config.IB_PORT = int(os.getenv('IB_PORT', config.IB_PORT))
    config.IB_CLIENT_ID = int(os.getenv('IB_CLIENT_ID', config.IB_CLIENT_ID))
    
    config.POSITION_SIZE = int(os.getenv('POSITION_SIZE', config.POSITION_SIZE))
    config.MAX_CONCURRENT_POSITIONS = int(os.getenv('MAX_CONCURRENT_POSITIONS', config.MAX_CONCURRENT_POSITIONS))
    config.TARGET_DELTA = float(os.getenv('TARGET_DELTA', config.TARGET_DELTA))
    
    config.PROFIT_TARGET_PCT = float(os.getenv('PROFIT_TARGET_PCT', config.PROFIT_TARGET_PCT))
    config.EXIT_DAY = int(os.getenv('EXIT_DAY', config.EXIT_DAY))
    
    config.DB_PATH = os.getenv('DB_PATH', config.DB_PATH)
    config.NOTIFY_TO_NUMBER = os.getenv('NOTIFY_TO_NUMBER', config.NOTIFY_TO_NUMBER)
    
    # Email-to-SMS settings
    config.SENDER_EMAIL = os.getenv('SENDER_EMAIL', config.SENDER_EMAIL)
    config.SENDER_APP_PASSWORD = os.getenv('SENDER_APP_PASSWORD', config.SENDER_APP_PASSWORD)
    config.SMS_GATEWAY = os.getenv('SMS_GATEWAY', config.SMS_GATEWAY)
    
    return config


# ===============================================
# SETUP INSTRUCTIONS
# ===============================================

SETUP_INSTRUCTIONS = """
🚀 SPX Double Calendar Spread Trading System Setup

1. IBKR Setup:
   - Install TWS or IB Gateway
   - Enable API connections in TWS settings
   - Set the port (7496 for live, 7497 for paper)
   - Make sure you have SPX options permissions

2. Python Environment:
   pip install -r requirements_spx_calendar.txt

3. SMS Notifications (FREE - No Twilio needed!):
   - Uses Gmail email-to-SMS gateway (completely free)
   - Set up Gmail app password (already done)
   - Set environment variables (optional):
     export SENDER_EMAIL="your_email@gmail.com"
     export SENDER_APP_PASSWORD="your_app_password"

4. Running the System:
   
   Automatic Mode (runs scheduler):
   python spx_double_calendar.py
   
   Manual Override Mode:
   python spx_double_calendar.py --mode manual
   
   Test Mode (single execution):
   python spx_double_calendar.py --mode test

5. Configuration:
   - Modify spx_calendar_config.py for your settings
   - Or set environment variables (see load_config_from_env)

6. Database:
   - SQLite database will be created automatically
   - Default location: spx_calendar_trades.db

7. Logging:
   - Logs are written to spx_calendar.log
   - Also displayed in console

⚠️ IMPORTANT SAFETY NOTES:
- Start with paper trading to test
- Verify all strikes and expiries before going live
- Monitor positions regularly
- The system will skip trades if conditions aren't met
- Use manual override when needed

📱 FREE SMS Notifications via Email-to-SMS will be sent for:
- Trade attempts and results
- Position closures  
- System errors
- Uses your existing Gmail account - no monthly fees!

🎯 Strategy Summary:
- Daily execution at 9:45 AM ET (M-F)
- Find 21-day and 28-day SPXW expiries
- Select ~20 delta strikes for puts and calls
- Sell 21-day, buy 28-day (calendar spread)
- 4-lot position size, max 7 concurrent positions
- Exit at 50% profit or 14th day at 3:00 PM
"""

if __name__ == "__main__":
    print(SETUP_INSTRUCTIONS)
