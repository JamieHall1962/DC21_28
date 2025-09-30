#!/usr/bin/env python3
"""
SPX Double Calendar Spread Automated Trading System
Executes daily at 9:45 AM M-F using IBKR API

Strategy:
- Find 21-day and 28-day SPXW expiration dates
- Select strikes closest to 20 delta for puts and calls
- Sell 21-day options, buy 28-day options at same strikes
- 4-lot position sizing, max 7 concurrent positions
- Exit at 50% profit or 15:00 on 14th day
"""

import asyncio
import sqlite3
import logging
import threading
import time
import schedule
import pandas as pd
import numpy as np
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
import pytz
import requests
import smtplib
import ssl

# IBKR API imports
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import TickerId
from ibapi.ticktype import TickTypeEnum

# ===============================================
# CONFIGURATION AND DATA MODELS
# ===============================================

@dataclass
class CalendarConfig:
    """System configuration for SPX Calendar spreads"""
    # IBKR Connection
    ib_host: str = "127.0.0.1"
    ib_port: int = 7496  # TWS live: 7496, paper: 7497
    ib_client_id: int = 2
    
    # Trading Parameters
    position_size: int = 4  # contracts per leg
    max_concurrent_positions: int = 7
    target_delta: float = 0.20
    delta_tolerance: float = 0.05  # acceptable range around target delta
    
    # Order Management
    initial_price_type: str = "MID"  # start with mid price
    price_increment: float = 0.05
    max_price_attempts: int = 5
    fill_wait_time: int = 60  # seconds between price adjustments
    max_spread_premium: float = 0.25  # Maximum additional premium above mid to pay
    
    # Exit Management
    profit_target_pct: float = 0.50  # 50% profit target
    exit_day: int = 14  # exit on 14th day at 15:00
    exit_time: dt_time = dt_time(15, 0)  # 3:00 PM ET
    
    # Schedule
    entry_time: dt_time = dt_time(9, 44, 50)  # 9:44:50 AM ET
    timezone: str = "America/New_York"
    
    # Database
    db_path: str = "spx_calendar_trades.db"
    
    # Email-to-SMS Notifications (Free alternative to Twilio)
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    sender_email: str = "hall.jamie@gmail.com"
    sender_app_password: str = "eima ybzz qlum grep"  # Gmail app password
    notify_to_number: str = "8133344846"  # Phone number for SMS
    sms_gateway: str = "@vtext.com"  # Xfinity/Verizon gateway
    
    def __post_init__(self):
        """Load settings from database after initialization"""
        self.load_from_database()
    
    def load_from_database(self):
        """Load configuration from database settings"""
        try:
            # Create a temporary database instance to load settings
            from pathlib import Path
            if Path(self.db_path).exists():
                db = CalendarDatabase(self.db_path)
                
                # Load configurable settings from database
                self.max_concurrent_positions = db.get_setting('max_concurrent_positions', self.max_concurrent_positions)
                self.position_size = db.get_setting('position_size', self.position_size)
                self.target_delta = db.get_setting('target_delta', self.target_delta)
                self.profit_target_pct = db.get_setting('profit_target_pct', self.profit_target_pct)
                self.exit_day = db.get_setting('exit_day', self.exit_day)
                self.notify_to_number = db.get_setting('notify_to_number', self.notify_to_number)
                self.ib_client_id = db.get_setting('ib_client_id', self.ib_client_id)
                self.ib_host = db.get_setting('ib_host', self.ib_host)
                self.ib_port = db.get_setting('ib_port', self.ib_port)
                self.price_increment = db.get_setting('price_increment', self.price_increment)
                self.max_price_attempts = db.get_setting('max_price_attempts', self.max_price_attempts)
                self.fill_wait_time = db.get_setting('fill_wait_time', self.fill_wait_time)
                self.max_spread_premium = db.get_setting('max_spread_premium', self.max_spread_premium)
                
                # Failed trade handling
                self.failed_trade_action = db.get_setting('failed_trade_action', 'skip')
                self.max_strike_deviation = db.get_setting('max_strike_deviation', 10)
                
        except Exception as e:
            print(f"Warning: Could not load settings from database: {e}")
            print("Using default configuration values")
    
    def save_to_database(self):
        """Save current configuration to database"""
        try:
            db = CalendarDatabase(self.db_path)
            
            # Save configurable settings to database
            db.set_setting('max_concurrent_positions', self.max_concurrent_positions)
            db.set_setting('position_size', self.position_size)
            db.set_setting('target_delta', self.target_delta)
            db.set_setting('profit_target_pct', self.profit_target_pct)
            db.set_setting('exit_day', self.exit_day)
            db.set_setting('notify_to_number', self.notify_to_number)
            db.set_setting('ib_client_id', self.ib_client_id)
            db.set_setting('ib_host', self.ib_host)
            db.set_setting('ib_port', self.ib_port)
            db.set_setting('price_increment', self.price_increment)
            db.set_setting('max_price_attempts', self.max_price_attempts)
            db.set_setting('fill_wait_time', self.fill_wait_time)
            db.set_setting('max_spread_premium', self.max_spread_premium)
            db.set_setting('failed_trade_action', self.failed_trade_action)
            db.set_setting('max_strike_deviation', self.max_strike_deviation)
            
        except Exception as e:
            print(f"Error: Could not save settings to database: {e}")

@dataclass
class CalendarSpread:
    """Individual calendar spread trade record"""
    trade_id: str
    entry_date: str
    entry_time: str
    spx_price: float
    short_expiry: str  # 21-day expiry
    long_expiry: str   # 28-day expiry
    put_strike: float      # Short put strike  
    call_strike: float     # Short call strike
    long_put_strike: float = 0.0   # Long put strike (may differ for adjustments)
    long_call_strike: float = 0.0  # Long call strike (may differ for adjustments)
    
    # Entry Greeks and IV
    entry_short_put_delta: float = 0.0
    entry_short_put_iv: float = 0.0
    entry_short_call_delta: float = 0.0
    entry_short_call_iv: float = 0.0
    entry_long_put_delta: float = 0.0
    entry_long_put_iv: float = 0.0
    entry_long_call_delta: float = 0.0
    entry_long_call_iv: float = 0.0
    
    # Exit Greeks and IV (populated on close)
    exit_short_put_delta: float = 0.0
    exit_short_put_iv: float = 0.0
    exit_short_call_delta: float = 0.0
    exit_short_call_iv: float = 0.0
    exit_long_put_delta: float = 0.0
    exit_long_put_iv: float = 0.0
    exit_long_call_delta: float = 0.0
    exit_long_call_iv: float = 0.0
    
    # Position details
    short_put_contract_id: int = 0
    short_call_contract_id: int = 0
    long_put_contract_id: int = 0
    long_call_contract_id: int = 0
    
    # Pricing
    entry_credit: float = 0.0
    exit_credit: float = 0.0   # Final closing price
    realized_pnl: float = 0.0  # Only on close
    profit_target: float = 0.0
    
    # Status
    status: str = "PENDING"  # PENDING, ACTIVE, CLOSED, CANCELLED
    exit_reason: str = ""
    exit_date: str = ""
    exit_time: str = ""
    exit_spx_price: float = 0.0
    
    # Order tracking
    combo_order_id: int = 0
    fill_status: str = "UNFILLED"
    fill_attempts: int = 0
    last_bid_price: float = 0.0
    
    # Profit target order tracking
    profit_target_order_id: int = 0
    profit_target_price: float = 0.0
    profit_target_status: str = "NONE"  # NONE, PLACED, FILLED, CANCELLED

# ===============================================
# DATABASE MANAGER
# ===============================================

class CalendarDatabase:
    """Manages SQLite database for calendar spread trades"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize database with required tables"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Main trades table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS calendar_trades (
                trade_id TEXT PRIMARY KEY,
                entry_date TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                spx_price REAL NOT NULL,
                short_expiry TEXT NOT NULL,
                long_expiry TEXT NOT NULL,
                            put_strike REAL NOT NULL,      -- Short put strike
            call_strike REAL NOT NULL,     -- Short call strike  
            long_put_strike REAL DEFAULT 0.0,   -- Long put strike (for adjustments)
            long_call_strike REAL DEFAULT 0.0,  -- Long call strike (for adjustments)
                
                -- Entry Greeks and IV
                entry_short_put_delta REAL DEFAULT 0.0,
                entry_short_put_iv REAL DEFAULT 0.0,
                entry_short_call_delta REAL DEFAULT 0.0,
                entry_short_call_iv REAL DEFAULT 0.0,
                entry_long_put_delta REAL DEFAULT 0.0,
                entry_long_put_iv REAL DEFAULT 0.0,
                entry_long_call_delta REAL DEFAULT 0.0,
                entry_long_call_iv REAL DEFAULT 0.0,
                
                -- Exit Greeks and IV (populated on close)
                exit_short_put_delta REAL DEFAULT 0.0,
                exit_short_put_iv REAL DEFAULT 0.0,
                exit_short_call_delta REAL DEFAULT 0.0,
                exit_short_call_iv REAL DEFAULT 0.0,
                exit_long_put_delta REAL DEFAULT 0.0,
                exit_long_put_iv REAL DEFAULT 0.0,
                exit_long_call_delta REAL DEFAULT 0.0,
                exit_long_call_iv REAL DEFAULT 0.0,
                
                short_put_contract_id INTEGER DEFAULT 0,
                short_call_contract_id INTEGER DEFAULT 0,
                long_put_contract_id INTEGER DEFAULT 0,
                long_call_contract_id INTEGER DEFAULT 0,
                
                entry_credit REAL DEFAULT 0.0,
                exit_credit REAL DEFAULT 0.0,  -- Final closing price
                realized_pnl REAL DEFAULT 0.0,  -- Only on close
                profit_target REAL DEFAULT 0.0,
                
                status TEXT DEFAULT 'PENDING',
                exit_reason TEXT DEFAULT '',
                exit_date TEXT DEFAULT '',
                exit_time TEXT DEFAULT '',
                exit_spx_price REAL DEFAULT 0.0,
                
                combo_order_id INTEGER DEFAULT 0,
                fill_status TEXT DEFAULT 'UNFILLED',
                fill_attempts INTEGER DEFAULT 0,
                last_bid_price REAL DEFAULT 0.0,
                
                -- Profit target order tracking
                profit_target_order_id INTEGER DEFAULT 0,
                profit_target_price REAL DEFAULT 0.0,
                profit_target_status TEXT DEFAULT 'NONE',
                
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Order history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS order_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                price REAL NOT NULL,
                status TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trade_id) REFERENCES calendar_trades (trade_id)
            )
        ''')
        
        # Daily log table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                action TEXT NOT NULL,
                message TEXT NOT NULL,
                success BOOLEAN NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # User settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                id INTEGER PRIMARY KEY,
                setting_name TEXT UNIQUE NOT NULL,
                setting_value TEXT NOT NULL,
                setting_type TEXT NOT NULL,  -- 'int', 'float', 'str', 'bool'
                description TEXT,
                category TEXT,
                min_value REAL,
                max_value REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create daily_actions table for reconciliation and system actions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                message TEXT NOT NULL,
                success BOOLEAN NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create command queue table for web interface communication
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS command_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_type TEXT NOT NULL,  -- 'CLOSE_POSITION', 'STOP_MANAGING', etc.
                trade_id TEXT,               -- Target trade ID (if applicable)
                parameters TEXT,             -- JSON parameters for command
                status TEXT DEFAULT 'PENDING',  -- 'PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'
                result TEXT DEFAULT '',      -- Result message or error
                created_by TEXT DEFAULT 'WEB_INTERFACE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP DEFAULT NULL
            )
        ''')
        
        # Insert default settings if they don't exist
        default_settings = [
            ('max_concurrent_positions', '7', 'int', 'Maximum number of concurrent positions', 'Trading', 1, 20),
            ('position_size', '4', 'int', 'Number of contracts per leg', 'Trading', 1, 50),
            ('target_delta', '0.20', 'float', 'Target delta for strike selection', 'Trading', 0.05, 0.50),
            ('profit_target_pct', '0.50', 'float', 'Profit target percentage', 'Trading', 0.10, 2.00),
            ('exit_day', '14', 'int', 'Exit day (days before short expiry)', 'Trading', 1, 21),
            ('failed_trade_action', 'skip', 'str', 'Action for failed trades: skip, adjust_longs, adjust_entire', 'Trading', None, None),
            ('max_strike_deviation', '10', 'int', 'Maximum strike deviation for adjustments (points)', 'Trading', 1, 50),
            ('ghost_strike_action', 'move', 'str', 'Action for ghost strikes: move, ignore, skip', 'Trading', None, None),
            ('notify_to_number', '8133344846', 'str', 'SMS notification phone number', 'Notifications', None, None),
            ('ib_client_id', '2', 'int', 'Interactive Brokers client ID', 'Connection', 1, 100),
            ('ib_host', '127.0.0.1', 'str', 'Interactive Brokers host', 'Connection', None, None),
            ('ib_port', '7496', 'int', 'Interactive Brokers port', 'Connection', 1000, 9999),
            ('price_increment', '0.05', 'float', 'Price increment for order adjustments', 'Orders', 0.01, 1.00),
            ('max_price_attempts', '5', 'int', 'Maximum price adjustment attempts', 'Orders', 1, 10),
            ('fill_wait_time', '60', 'int', 'Wait time between price adjustments (seconds)', 'Orders', 10, 300),
            ('max_spread_premium', '0.25', 'float', 'Maximum additional premium above mid to pay', 'Orders', 0.05, 1.00)
        ]
        
        for setting_name, default_value, setting_type, description, category, min_val, max_val in default_settings:
            cursor.execute('''
                INSERT OR IGNORE INTO user_settings 
                (setting_name, setting_value, setting_type, description, category, min_value, max_value)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (setting_name, default_value, setting_type, description, category, min_val, max_val))
        
        # Add profit target order columns if they don't exist (migration)
        try:
            cursor.execute('ALTER TABLE calendar_trades ADD COLUMN profit_target_order_id INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # Column already exists
            
        try:
            cursor.execute('ALTER TABLE calendar_trades ADD COLUMN profit_target_price REAL DEFAULT 0.0')
        except sqlite3.OperationalError:
            pass  # Column already exists
            
        try:
            cursor.execute('ALTER TABLE calendar_trades ADD COLUMN profit_target_status TEXT DEFAULT "NONE"')
        except sqlite3.OperationalError:
            pass  # Column already exists

        conn.commit()
        conn.close()
        print("✓ Calendar spread database initialized")
    
    def save_trade(self, trade: CalendarSpread):
        """Save or update a calendar spread trade"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Convert dataclass to dict for database insertion
        trade_dict = asdict(trade)
        trade_dict['updated_at'] = datetime.now().isoformat()
        
        # Use REPLACE to handle both insert and update
        placeholders = ', '.join(['?' for _ in trade_dict])
        columns = ', '.join(trade_dict.keys())
        
        cursor.execute(f'''
            REPLACE INTO calendar_trades ({columns})
            VALUES ({placeholders})
        ''', list(trade_dict.values()))
        
        conn.commit()
        conn.close()
    
    def get_active_trades(self) -> List[CalendarSpread]:
        """Get all active calendar spread trades"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM calendar_trades 
            WHERE status IN ('PENDING', 'ACTIVE', 'MANUAL_CONTROL')
            ORDER BY entry_date DESC
        ''')
        
        trades = []
        for row in cursor.fetchall():
            # Convert row to CalendarSpread object
            trade_dict = dict(zip([col[0] for col in cursor.description], row))
            # Remove database-specific fields
            trade_dict.pop('created_at', None)
            trade_dict.pop('updated_at', None)
            trades.append(CalendarSpread(**trade_dict))
        
        conn.close()
        return trades
    
    def get_trade_by_id(self, trade_id: str) -> Optional[CalendarSpread]:
        """Get a specific trade by ID"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM calendar_trades 
            WHERE trade_id = ?
        ''', (trade_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            # Convert row to CalendarSpread object
            trade_dict = dict(zip([col[0] for col in cursor.description], row))
            # Remove database-specific fields
            trade_dict.pop('created_at', None)
            trade_dict.pop('updated_at', None)
            return CalendarSpread(**trade_dict)
        
        return None
    
    def get_trade_count_for_date(self, date: str) -> int:
        """Get number of trades entered on specific date"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT COUNT(*) FROM calendar_trades 
            WHERE entry_date = ? AND status != 'CANCELLED'
        ''', (date,))
        
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    def get_total_trade_count(self) -> int:
        """Get total count of all trades"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM calendar_trades WHERE status != "CANCELLED"')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    def get_setting(self, setting_name: str, default_value=None):
        """Get a setting value from the database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT setting_value, setting_type FROM user_settings 
            WHERE setting_name = ?
        ''', (setting_name,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            value, setting_type = result
            # Convert to appropriate type
            if setting_type == 'int':
                return int(value)
            elif setting_type == 'float':
                return float(value)
            elif setting_type == 'bool':
                return value.lower() in ('true', '1', 'yes', 'on')
            else:  # str
                return value
        else:
            return default_value
    
    def set_setting(self, setting_name: str, setting_value, setting_type: str = None):
        """Set a setting value in the database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Auto-detect type if not provided
        if setting_type is None:
            if isinstance(setting_value, int):
                setting_type = 'int'
            elif isinstance(setting_value, float):
                setting_type = 'float'
            elif isinstance(setting_value, bool):
                setting_type = 'bool'
            else:
                setting_type = 'str'
        
        # Convert value to string for storage
        str_value = str(setting_value)
        
        cursor.execute('''
            UPDATE user_settings 
            SET setting_value = ?, setting_type = ?, updated_at = CURRENT_TIMESTAMP
            WHERE setting_name = ?
        ''', (str_value, setting_type, setting_name))
        
        conn.commit()
        conn.close()
    
    def get_all_settings(self):
        """Get all settings organized by category"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT setting_name, setting_value, setting_type, description, 
                   category, min_value, max_value
            FROM user_settings 
            ORDER BY category, setting_name
        ''')
        
        results = cursor.fetchall()
        conn.close()
        
        # Organize by category
        settings_by_category = {}
        for row in results:
            setting_name, value, setting_type, description, category, min_val, max_val = row
            
            if category not in settings_by_category:
                settings_by_category[category] = []
            
            # Convert value to appropriate type
            if setting_type == 'int':
                typed_value = int(value)
            elif setting_type == 'float':
                typed_value = float(value)
            elif setting_type == 'bool':
                typed_value = value.lower() in ('true', '1', 'yes', 'on')
            else:
                typed_value = value
            
            settings_by_category[category].append({
                'name': setting_name,
                'value': typed_value,
                'raw_value': value,
                'type': setting_type,
                'description': description,
                'min_value': min_val,
                'max_value': max_val
            })
        
        return settings_by_category
    
    def log_daily_action(self, action: str, message: str, success: bool = True):
        """Log daily trading actions"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Use Eastern Time for logs
        eastern_tz = pytz.timezone('US/Eastern')
        eastern_time = datetime.now(eastern_tz)
        
        cursor.execute('''
            INSERT INTO daily_log (date, action, message, success)
            VALUES (?, ?, ?, ?)
        ''', (eastern_time.strftime('%Y-%m-%d %H:%M:%S'), action, message, success))
        
        conn.commit()
        conn.close()
    
    # ===============================================
    # COMMAND QUEUE METHODS (for web interface communication)
    # ===============================================
    
    def add_command(self, command_type: str, trade_id: str = None, parameters: str = None) -> int:
        """Add a command to the queue for the main system to process"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO command_queue (command_type, trade_id, parameters)
            VALUES (?, ?, ?)
        ''', (command_type, trade_id, parameters))
        
        command_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return command_id
    
    def get_pending_commands(self):
        """Get all pending commands from the queue"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, command_type, trade_id, parameters, created_at
            FROM command_queue 
            WHERE status = 'PENDING'
            ORDER BY created_at ASC
        ''')
        
        commands = cursor.fetchall()
        conn.close()
        
        return commands
    
    def update_command_status(self, command_id: int, status: str, result: str = ''):
        """Update command status and result"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE command_queue 
            SET status = ?, result = ?, processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (status, result, command_id))
        
        conn.commit()
        conn.close()
    
    def cleanup_old_commands(self, days_old: int = 7):
        """Clean up old completed/failed commands"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM command_queue 
            WHERE status IN ('COMPLETED', 'FAILED') 
            AND datetime(created_at) < datetime('now', '-' || ? || ' days')
        ''', (days_old,))
        
        conn.commit()
        conn.close()

# ===============================================
# NOTIFICATION MANAGER
# ===============================================

class NotificationManager:
    """Handles SMS notifications via Email-to-SMS gateway (Free alternative to Twilio)"""
    
    def __init__(self, config: CalendarConfig):
        self.config = config
        
        # Validate email configuration
        if config.sender_email and config.sender_app_password:
            print("✓ Email-to-SMS notification system initialized")
        else:
            print("⚠️ Email credentials not provided - SMS notifications disabled")
    
    def send_sms(self, message: str) -> bool:
        """Send SMS via email-to-SMS gateway"""
        if not self.config.sender_email or not self.config.sender_app_password:
            print(f"📱 SMS (not sent - no email config): {message}")
            return False
        
        try:
            # Create SMS gateway email address
            sms_email = f"{self.config.notify_to_number}{self.config.sms_gateway}"
            
            # Create email message for SMS
            email_body = f"From: {self.config.sender_email}\nTo: {sms_email}\nSubject: \n\n{message}"
            
            # Send via SMTP
            context = ssl.create_default_context()
            with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port) as server:
                server.starttls(context=context)
                server.login(self.config.sender_email, self.config.sender_app_password)
                server.sendmail(self.config.sender_email, sms_email, email_body)
            
            print(f"✓ SMS sent: {message}")
            return True
            
        except Exception as e:
            print(f"SMS failed: {e}")
            return False
    
    def notify_trade_attempt(self, spx_price: float, put_strike: float, call_strike: float, 
                           short_expiry: str, long_expiry: str, starting_bid: float):
        """Notify about trade attempt with starting bid"""
        message = (f"SPX Calendar: Attempting trade at {spx_price:.2f}. "
                  f"Strikes: {put_strike:.0f}P/{call_strike:.0f}C. "
                  f"Starting bid: ${starting_bid:.2f}. "
                  f"Expiry: {short_expiry}/{long_expiry}")
        self.send_sms(message)
    
    def notify_trade_filled(self, trade: CalendarSpread):
        """Notify about successful trade fill"""
        message = (f"SPX Calendar FILLED: {trade.entry_credit:.2f} debit. "
                  f"Target: {trade.profit_target:.2f}. "
                  f"Strikes: {trade.put_strike:.0f}/{trade.call_strike:.0f}")
        self.send_sms(message)
    
    def notify_trade_failed(self, reason: str, spx_price: float = None, 
                           put_strike: float = None, call_strike: float = None,
                           short_expiry: str = None, long_expiry: str = None):
        """Notify about failed trade with detailed context"""
        message = f"SPX Calendar FAILED: {reason}"
        
        # Add context if provided
        if spx_price:
            message += f" SPX@{spx_price:.2f}"
        
        if short_expiry and long_expiry:
            # Format expiry dates nicely (YYYYMMDD -> MM/DD)
            try:
                short_date = f"{short_expiry[4:6]}/{short_expiry[6:8]}"
                long_date = f"{long_expiry[4:6]}/{long_expiry[6:8]}"
                message += f" Exp:{short_date},{long_date}"
            except:
                message += f" Exp:{short_expiry},{long_expiry}"
        
        if put_strike and call_strike:
            message += f" Strikes:{put_strike:.0f}P/{call_strike:.0f}C"
        
        self.send_sms(message)
    
    def notify_position_closed(self, trade: CalendarSpread, pnl: float):
        """Notify about position closure"""
        message = (f"SPX Calendar CLOSED: {trade.exit_reason}. "
                  f"P&L: {pnl:.2f} ({pnl/abs(trade.entry_credit)*100:.1f}%)")
        self.send_sms(message)

# ===============================================
# IBKR WRAPPER AND CLIENT
# ===============================================

class CalendarWrapper(EWrapper):
    """IBKR API Wrapper for calendar spread trading"""
    
    def __init__(self):
        EWrapper.__init__(self)
        self.next_order_id = None
        self.contract_details = {}
        self.market_data = {}
        self.option_chains = {}
        self.positions = {}
        self.orders = {}
        self.connection_errors = []
        
        # Streaming market data
        self.streaming_data = {}  # req_id -> {symbol, price, bid, ask, last_update}
        self.streaming_callbacks = {}  # req_id -> callback function
        
        # Events for synchronization
        self.contract_details_received = threading.Event()
        self.market_data_received = threading.Event()
        self.order_status_received = threading.Event()
    
    def nextValidId(self, orderId: int):
        """Receive next valid order ID"""
        self.next_order_id = orderId
        print(f"✓ Next valid order ID: {orderId}")
    
    def contractDetails(self, reqId: int, contractDetails):
        """Receive contract details"""
        if reqId not in self.contract_details:
            self.contract_details[reqId] = []
        self.contract_details[reqId].append(contractDetails)
    
    def contractDetailsEnd(self, reqId: int):
        """Contract details request completed"""
        print(f"✓ Contract details received for request {reqId}")
        self.contract_details_received.set()
    
    def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib):
        """Receive market data tick"""
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        
        # Store in regular market data (for backward compatibility)
        if tickType == TickTypeEnum.BID:
            self.market_data[reqId]['bid'] = price
        elif tickType == TickTypeEnum.ASK:
            self.market_data[reqId]['ask'] = price
        elif tickType == TickTypeEnum.LAST:
            self.market_data[reqId]['last'] = price
        elif tickType == TickTypeEnum.CLOSE:
            self.market_data[reqId]['close'] = price
        
        # Handle streaming data updates
        if reqId in self.streaming_data:
            stream_data = self.streaming_data[reqId]
            updated = False
            
            if tickType == TickTypeEnum.BID:
                stream_data['bid'] = price
                updated = True
            elif tickType == TickTypeEnum.ASK:
                stream_data['ask'] = price
                updated = True
            elif tickType == TickTypeEnum.LAST:
                stream_data['last'] = price
                stream_data['price'] = price  # Use last as primary price
                updated = True
            elif tickType == TickTypeEnum.CLOSE:
                if 'price' not in stream_data or stream_data['price'] == 0:
                    stream_data['price'] = price  # Use close if no last price
                updated = True
            
            if updated:
                stream_data['last_update'] = time.time()
                
                # Calculate mid price if we have bid/ask
                if 'bid' in stream_data and 'ask' in stream_data:
                    stream_data['mid'] = (stream_data['bid'] + stream_data['ask']) / 2
                
                # Call callback if registered
                if reqId in self.streaming_callbacks:
                    try:
                        self.streaming_callbacks[reqId](reqId, stream_data)
                    except Exception as e:
                        print(f"Error in streaming callback for {reqId}: {e}")
    
    def tickOptionComputation(self, reqId: TickerId, tickType: int, tickAttrib: int,
                            impliedVol: float, delta: float, optPrice: float,
                            pvDividend: float, gamma: float, vega: float,
                            theta: float, undPrice: float):
        """Receive option Greeks"""
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        
        if tickType == TickTypeEnum.MODEL_OPTION:
            self.market_data[reqId]['delta'] = delta
            self.market_data[reqId]['gamma'] = gamma
            self.market_data[reqId]['theta'] = theta
            self.market_data[reqId]['vega'] = vega
            self.market_data[reqId]['implied_vol'] = impliedVol
    
    def orderStatus(self, orderId: int, status: str, filled: float, remaining: float,
                   avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float,
                   clientId: int, whyHeld: str, mktCapPrice: float):
        """Receive order status updates"""
        self.orders[orderId] = {
            'status': status,
            'filled': filled,
            'remaining': remaining,
            'avg_fill_price': avgFillPrice,
            'last_fill_price': lastFillPrice
        }
        print(f"📋 Order {orderId}: {status}, Filled: {filled}, Price: {avgFillPrice}")
        
        # Also write to debug file for closing orders
        if orderId > 1000:  # Closing orders use high IDs
            with open('web_debug.log', 'a') as f:
                f.write(f"  ORDER STATUS: {orderId} -> {status}, Filled: {filled}, Price: {avgFillPrice}\n")
        
        # Check if this is a profit target order that got filled
        if status == "Filled" and hasattr(self, 'trader') and self.trader:
            self.trader.check_profit_target_fill(orderId, avgFillPrice)
        
        self.order_status_received.set()
    
    def position(self, account: str, contract: Contract, position: float, avgCost: float):
        """Receive position updates"""
        key = f"{contract.symbol}-{contract.secType}-{contract.lastTradeDateOrContractMonth}-{contract.strike}-{contract.right}"
        self.positions[key] = {
            'symbol': contract.symbol,
            'position': position,
            'avg_cost': avgCost,
            'contract': contract
        }
    
    def error(self, reqId: TickerId, errorCode: int, errorString: str, advancedOrderRejectJson: str = ""):
        """Handle errors"""
        print(f"Error {errorCode}: {errorString}")
        
        # Store only ACTUAL connection errors for reconnection logic
        # IBKR sends many "Error" messages that are actually status updates:
        # - 2104: "Market data farm connection is OK" (not an error!)
        # - 2106: "HMDS data farm connection is OK" (not an error!)
        # - 2158: "Sec-def data farm connection is OK" (not an error!)
        # Only 504 "Not connected" is a real disconnection error
        if errorCode in [504]:  # Only real disconnection errors
            self.connection_errors.append({
                'code': errorCode,
                'message': errorString,
                'timestamp': time.time()
            })

# ===============================================
# MAIN TRADING ENGINE
# ===============================================

class SPXCalendarTrader:
    """Main trading engine for SPX Double Calendar spreads"""
    
    def __init__(self, config: CalendarConfig):
        self.config = config
        self.db = CalendarDatabase(config.db_path)
        self.notifications = NotificationManager(config)
        
        # IBKR connection
        self.wrapper = CalendarWrapper()
        self.wrapper.trader = self  # Set reference for profit target handling
        self.client = EClient(self.wrapper)
        
        # Trading state
        self.current_spx_price = 0.0
        self.req_id_counter = 1000
        
        # Streaming market data
        self.streaming_positions = {}  # trade_id -> {req_ids: [], contracts: [], last_pnl: float}
        self.spx_stream_req_id = None
        
        # Set up Eastern Time zone for proper timestamps
        self.eastern_tz = pytz.timezone('US/Eastern')
        
        # Setup logging with Eastern Time
        import logging
        
        class EasternFormatter(logging.Formatter):
            def converter(self, timestamp):
                # Convert to Eastern Time
                from datetime import datetime
                import pytz
                dt = datetime.fromtimestamp(timestamp)
                eastern = pytz.timezone('US/Eastern')
                return dt.replace(tzinfo=pytz.UTC).astimezone(eastern).timetuple()
        
        formatter = EasternFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        # File handler
        file_handler = logging.FileHandler('spx_calendar.log')
        file_handler.setFormatter(formatter)
        
        # Console handler  
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        logging.basicConfig(
            level=logging.INFO,
            handlers=[file_handler, console_handler]
        )
        self.logger = logging.getLogger(__name__)
    
    def get_local_time(self):
        """Get current Eastern Time (market time)"""
        return datetime.now(self.eastern_tz)
    
    def disconnect_from_ibkr(self):
        """Properly disconnect from Interactive Brokers"""
        try:
            if self.client and self.client.isConnected():
                self.logger.info("Disconnecting from Interactive Brokers...")
                self.client.disconnect()
                time.sleep(2)  # Give time for proper cleanup
                self.is_connected = False
                self.logger.info("Disconnected from Interactive Brokers")
        except Exception as e:
            self.logger.error(f"Error during disconnect: {e}")

    def connect_to_ibkr(self) -> bool:
        """Connect to Interactive Brokers"""
        try:
            # Check if already connected - DON'T disconnect if working!
            if self.client and self.client.isConnected():
                self.is_connected = True
                self.logger.info("Already connected to Interactive Brokers")
                return True
            
            self.logger.info(f"Connecting to IBKR with Client ID {self.config.ib_client_id}...")
            self.client.connect(self.config.ib_host, self.config.ib_port, self.config.ib_client_id)
            
            # Start client thread
            api_thread = threading.Thread(target=self.client.run, daemon=True)
            api_thread.start()
            
            # Wait for connection
            time.sleep(3)
            
            if self.client.isConnected():
                self.is_connected = True
                self.logger.info("Connected to Interactive Brokers")
                return True
            else:
                self.is_connected = False
                self.logger.error("Failed to connect to Interactive Brokers")
                return False
                
        except Exception as e:
            self.logger.error(f"Connection error: {e}")
            self.is_connected = False
            return False
    
    @property
    def is_connected(self):
        """Check if currently connected to IBKR (real-time check)"""
        return self.client.isConnected() if self.client else False
    
    @is_connected.setter
    def is_connected(self, value):
        """Setter for is_connected (for backwards compatibility)"""
        self._is_connected = value
    
    
    def reconnect_to_ibkr(self) -> bool:
        """Reconnect to Interactive Brokers after connection loss"""
        try:
            self.logger.info("Disconnecting existing connection...")
            self.disconnect_from_ibkr()
            
            # Wait longer for IBKR to fully release the client ID
            self.logger.info("Waiting for IBKR to release client ID...")
            time.sleep(10)  # Increased wait time
            
            # Create new client and wrapper instances
            self.wrapper = CalendarWrapper()
            self.wrapper.trader = self  # Set reference for profit target handling
            self.client = EClient(self.wrapper)
            
            # Try with different client IDs if the original is still in use
            original_client_id = self.config.ib_client_id
            max_attempts = 3
            
            for attempt in range(max_attempts):
                # Try with incremented client ID if not first attempt
                if attempt > 0:
                    self.config.ib_client_id = original_client_id + attempt
                    self.logger.info(f"Attempting reconnection with client ID {self.config.ib_client_id}")
                else:
                    self.logger.info(f"Attempting reconnection with original client ID {self.config.ib_client_id}")
                
                success = self.connect_to_ibkr()
                
                if success:
                    self.logger.info(f"Successfully reconnected to IBKR with client ID {self.config.ib_client_id}")
                    return True
                else:
                    self.logger.warning(f"Reconnection attempt {attempt + 1} failed with client ID {self.config.ib_client_id}")
                    if attempt < max_attempts - 1:
                        time.sleep(5)  # Wait before trying next client ID
            
            # Restore original client ID for future attempts
            self.config.ib_client_id = original_client_id
            self.logger.error("Failed to reconnect to IBKR after all attempts")
            return False
                
        except Exception as e:
            self.logger.error(f"Reconnection error: {e}")
            return False
    
    def get_next_req_id(self) -> int:
        """Get next request ID"""
        req_id = self.req_id_counter
        self.req_id_counter += 1
        return req_id
    
    def get_spx_price(self) -> float:
        """Get current SPX index price with auto-reconnect on failure"""
        try:
            # First check if we have recent connection errors
            if self.has_recent_connection_errors():
                self.logger.warning("Recent connection errors detected, attempting reconnection...")
                self.reconnect_to_ibkr()
            
            return self._get_spx_price_internal()
        except Exception as e:
            # Check if it's a connection error that requires reconnection
            error_msg = str(e).lower()
            if any(err in error_msg for err in ['504', 'not connected', 'socket closed', 'connection lost', 'disconnected']):
                self.logger.warning(f"Connection issue detected: {e}")
                self.logger.info("Attempting to reconnect to IBKR...")
                
                # Mark as disconnected and attempt reconnection
                self.is_connected = False
                if self.reconnect_to_ibkr():
                    # Try getting price again after reconnection
                    try:
                        return self._get_spx_price_internal()
                    except Exception as retry_e:
                        self.logger.error(f"Failed to get SPX price after reconnection: {retry_e}")
                        raise retry_e
                else:
                    raise Exception("Failed to reconnect to IBKR")
            else:
                # Not a connection error, re-raise original exception
                raise e
    
    def has_recent_connection_errors(self) -> bool:
        """Check if there are recent connection errors (within last 30 seconds)"""
        current_time = time.time()
        recent_errors = [err for err in self.wrapper.connection_errors 
                        if current_time - err['timestamp'] < 30]
        return len(recent_errors) > 0
    
    def _get_spx_price_internal(self) -> float:
        """Internal method to get SPX price (without reconnection logic)"""
        # Create SPX index contract
        spx_contract = Contract()
        spx_contract.symbol = "SPX"
        spx_contract.secType = "IND"
        spx_contract.exchange = "CBOE"
        spx_contract.currency = "USD"
        
        req_id = self.get_next_req_id()
        self.client.reqMktData(req_id, spx_contract, "", False, False, [])
        
        # Wait for market data - try multiple price types
        timeout = 10
        start_time = time.time()
        while time.time() - start_time < timeout:
            if req_id in self.wrapper.market_data:
                data = self.wrapper.market_data[req_id]
                
                # Try different price types in order of preference
                for price_type in ['last', 'close', 'bid', 'ask']:
                    if price_type in data and data[price_type] > 0:
                        price = data[price_type]
                        self.client.cancelMktData(req_id)
                        self.logger.info(f"SPX price ({price_type}): {price:.2f}")
                        return price
            time.sleep(0.1)
        
        self.client.cancelMktData(req_id)
        raise Exception("Failed to get SPX price")
    
    def start_spx_streaming(self, callback=None):
        """Start streaming SPX price updates"""
        if self.spx_stream_req_id is not None:
            self.logger.info("SPX streaming already active")
            return self.spx_stream_req_id
        
        # Create SPX index contract
        spx_contract = Contract()
        spx_contract.symbol = "SPX"
        spx_contract.secType = "IND"
        spx_contract.exchange = "CBOE"
        spx_contract.currency = "USD"
        
        req_id = self.get_next_req_id()
        self.spx_stream_req_id = req_id
        
        # Initialize streaming data
        self.wrapper.streaming_data[req_id] = {
            'symbol': 'SPX',
            'price': 0.0,
            'bid': 0.0,
            'ask': 0.0,
            'last_update': 0
        }
        
        # Set callback for SPX updates
        if callback:
            self.wrapper.streaming_callbacks[req_id] = callback
        else:
            # Default callback updates current_spx_price
            def default_spx_callback(req_id, data):
                if 'price' in data and data['price'] > 0:
                    self.current_spx_price = data['price']
            self.wrapper.streaming_callbacks[req_id] = default_spx_callback
        
        # Start streaming
        self.client.reqMktData(req_id, spx_contract, "", False, False, [])
        self.logger.info(f"Started SPX streaming with req_id {req_id}")
        return req_id
    
    def start_position_streaming(self, trade: 'CalendarSpread', pnl_callback=None):
        """Start streaming market data for a calendar spread position"""
        if trade.trade_id in self.streaming_positions:
            self.logger.info(f"Position {trade.trade_id} already streaming")
            return
        
        # Create the 4 option contracts using actual executed strikes
        # For adjusted trades, use the stored long strikes; otherwise use short strikes
        actual_long_put = trade.long_put_strike if trade.long_put_strike > 0 else trade.put_strike
        actual_long_call = trade.long_call_strike if trade.long_call_strike > 0 else trade.call_strike
        
        short_put = self.create_spxw_contract(trade.short_expiry, trade.put_strike, "P")
        short_call = self.create_spxw_contract(trade.short_expiry, trade.call_strike, "C")
        long_put = self.create_spxw_contract(trade.long_expiry, actual_long_put, "P")
        long_call = self.create_spxw_contract(trade.long_expiry, actual_long_call, "C")
        
        contracts = [short_put, short_call, long_put, long_call]
        req_ids = []
        
        # Start streaming for each contract
        for i, contract in enumerate(contracts):
            req_id = self.get_next_req_id()
            req_ids.append(req_id)
            
            # Initialize streaming data
            symbol_key = f"{contract.symbol}_{contract.lastTradeDateOrContractMonth}_{contract.strike}_{contract.right}"
            self.wrapper.streaming_data[req_id] = {
                'symbol': symbol_key,
                'price': 0.0,
                'bid': 0.0,
                'ask': 0.0,
                'mid': 0.0,
                'last_update': 0,
                'contract_index': i  # 0=short_put, 1=short_call, 2=long_put, 3=long_call
            }
            
            # Start streaming with Greeks
            self.client.reqMktData(req_id, contract, "106", False, False, [])  # Include Greeks
            time.sleep(0.1)  # Avoid overwhelming IBKR
        
        # Store position streaming info
        self.streaming_positions[trade.trade_id] = {
            'req_ids': req_ids,
            'contracts': contracts,
            'last_pnl': 0.0,
            'entry_debit': trade.entry_credit
        }
        
        # Set up P&L calculation callback
        def calculate_pnl():
            spread_value = 0.0
            all_data_ready = True
            
            for i, req_id in enumerate(req_ids):
                if req_id in self.wrapper.streaming_data:
                    data = self.wrapper.streaming_data[req_id]
                    if 'mid' in data and data['mid'] > 0:
                        mid_price = data['mid']
                        # Double Calendar spread closing value:
                        if i < 2:  # Short legs (we sold these originally, now buy back)
                            spread_value -= mid_price  # We pay to buy back shorts
                        else:  # Long legs (we bought these originally, now sell)
                            spread_value += mid_price  # We receive for selling longs
                    else:
                        all_data_ready = False
                        break
                else:
                    all_data_ready = False
                    break
            
            if all_data_ready:
                # Round to nearest nickel
                spread_value = round(spread_value * 20) / 20
                pnl = spread_value - trade.entry_credit
                
                # Update stored P&L (in memory only)
                self.streaming_positions[trade.trade_id]['last_pnl'] = pnl
                self.streaming_positions[trade.trade_id]['current_spread_value'] = spread_value
                
                # Call custom callback if provided
                if pnl_callback:
                    pnl_callback(trade.trade_id, spread_value, pnl)
        
        # Set callback for any price update to trigger P&L calculation
        for req_id in req_ids:
            self.wrapper.streaming_callbacks[req_id] = lambda rid, data: calculate_pnl()
        
        self.logger.info(f"Started streaming for position {trade.trade_id} with {len(req_ids)} contracts")
    
    def stop_position_streaming(self, trade_id: str):
        """Stop streaming market data for a position"""
        if trade_id not in self.streaming_positions:
            return
        
        position_info = self.streaming_positions[trade_id]
        for req_id in position_info['req_ids']:
            self.client.cancelMktData(req_id)
            # Clean up streaming data
            if req_id in self.wrapper.streaming_data:
                del self.wrapper.streaming_data[req_id]
            if req_id in self.wrapper.streaming_callbacks:
                del self.wrapper.streaming_callbacks[req_id]
        
        del self.streaming_positions[trade_id]
        self.logger.info(f"Stopped streaming for position {trade_id}")
    
    def stop_spx_streaming(self):
        """Stop SPX streaming"""
        if self.spx_stream_req_id is not None:
            self.client.cancelMktData(self.spx_stream_req_id)
            if self.spx_stream_req_id in self.wrapper.streaming_data:
                del self.wrapper.streaming_data[self.spx_stream_req_id]
            if self.spx_stream_req_id in self.wrapper.streaming_callbacks:
                del self.wrapper.streaming_callbacks[self.spx_stream_req_id]
            self.spx_stream_req_id = None
            self.logger.info("Stopped SPX streaming")
    
    def get_streaming_pnl(self, trade_id: str) -> Optional[Tuple[float, float]]:
        """Get current P&L from streaming data (spread_value, pnl)"""
        if trade_id in self.streaming_positions:
            position_info = self.streaming_positions[trade_id]
            
            # Use cached values if available (updated on every tick)
            if 'current_spread_value' in position_info and 'last_pnl' in position_info:
                return position_info['current_spread_value'], position_info['last_pnl']
            
            # Fallback to real-time calculation
            spread_value = 0.0
            all_data_ready = True
            
            for i, req_id in enumerate(position_info['req_ids']):
                if req_id in self.wrapper.streaming_data:
                    data = self.wrapper.streaming_data[req_id]
                    if 'mid' in data and data['mid'] > 0:
                        mid_price = data['mid']
                        if i < 2:  # Short legs
                            spread_value -= mid_price
                        else:  # Long legs
                            spread_value += mid_price
                    else:
                        all_data_ready = False
                        break
                else:
                    all_data_ready = False
                    break
            
            if all_data_ready:
                spread_value = round(spread_value * 20) / 20
                pnl = spread_value - position_info['entry_debit']
                return spread_value, pnl
        
        return None
    
    def calculate_expiry_dates(self) -> Tuple[str, str]:
        """Calculate 21-day and 28-day expiry dates"""
        today = self.get_local_time()  # Use Eastern Time, not UTC
        
        self.logger.info(f"Base date for expiry calculation: {today.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        
        # Calculate target dates
        date_21 = today + timedelta(days=21)
        date_28 = today + timedelta(days=28)
        
        # Format as YYYYMMDD for IBKR
        expiry_21 = date_21.strftime('%Y%m%d')
        expiry_28 = date_28.strftime('%Y%m%d')
        
        self.logger.info(f"Calculated expiries: {expiry_21} (21d), {expiry_28} (28d)")
        
        return expiry_21, expiry_28
    
    def create_spxw_contract(self, expiry: str, strike: float, right: str) -> Contract:
        """Create SPXW option contract"""
        contract = Contract()
        contract.symbol = "SPX"
        contract.secType = "OPT"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contract.lastTradeDateOrContractMonth = expiry
        contract.strike = strike
        contract.right = right  # "C" or "P"
        contract.multiplier = "100"
        contract.tradingClass = "SPXW"
        
        return contract
    
    def _capture_entry_greeks(self, trade: CalendarSpread, market_data: dict):
        """Capture Greeks and IV for all legs at trade entry"""
        try:
            # market_data is a dict: {0: req_id, 1: req_id, 2: req_id, 3: req_id}
            # 0: short_put, 1: short_call, 2: long_put, 3: long_call
            
            leg_names = ['short_put', 'short_call', 'long_put', 'long_call']
            
            for i, leg_name in enumerate(leg_names):
                if i in market_data:
                    req_id = market_data[i]
                    if req_id in self.wrapper.market_data:
                        data = self.wrapper.market_data[req_id]
                        delta = data.get('delta', 0.0)
                        iv = data.get('implied_vol', 0.0)
                        
                        # Store in trade object
                        if leg_name == 'short_put':
                            trade.entry_short_put_delta = delta
                            trade.entry_short_put_iv = iv
                        elif leg_name == 'short_call':
                            trade.entry_short_call_delta = delta
                            trade.entry_short_call_iv = iv
                        elif leg_name == 'long_put':
                            trade.entry_long_put_delta = delta
                            trade.entry_long_put_iv = iv
                        elif leg_name == 'long_call':
                            trade.entry_long_call_delta = delta
                            trade.entry_long_call_iv = iv
                        
                        self.logger.info(f"Entry Greeks captured - {leg_name}: Delta={delta:.3f}, IV={iv:.3f}")
                    else:
                        self.logger.warning(f"No market data available for {leg_name} (req_id: {req_id})")
                else:
                    self.logger.warning(f"No req_id found for {leg_name} (index: {i})")
                    
            # Cancel market data requests to avoid unnecessary data
            for req_id in market_data.values():
                self.client.cancelMktData(req_id)
                
        except Exception as e:
            self.logger.error(f"Error capturing entry Greeks: {e}")
    
    def _capture_exit_greeks(self, trade: CalendarSpread, market_data: dict):
        """Capture Greeks and IV for all legs at trade exit"""
        try:
            # market_data is a dict: {0: req_id, 1: req_id, 2: req_id, 3: req_id}
            # 0: short_put, 1: short_call, 2: long_put, 3: long_call
            
            leg_names = ['short_put', 'short_call', 'long_put', 'long_call']
            
            for i, leg_name in enumerate(leg_names):
                if i in market_data:
                    req_id = market_data[i]
                    if req_id in self.wrapper.market_data:
                        data = self.wrapper.market_data[req_id]
                        delta = data.get('delta', 0.0)
                        iv = data.get('implied_vol', 0.0)
                        
                        # Store in trade object
                        if leg_name == 'short_put':
                            trade.exit_short_put_delta = delta
                            trade.exit_short_put_iv = iv
                        elif leg_name == 'short_call':
                            trade.exit_short_call_delta = delta
                            trade.exit_short_call_iv = iv
                        elif leg_name == 'long_put':
                            trade.exit_long_put_delta = delta
                            trade.exit_long_put_iv = iv
                        elif leg_name == 'long_call':
                            trade.exit_long_call_delta = delta
                            trade.exit_long_call_iv = iv
                        
                        self.logger.info(f"Exit Greeks captured - {leg_name}: Delta={delta:.3f}, IV={iv:.3f}")
                    else:
                        self.logger.warning(f"No market data available for {leg_name} at exit (req_id: {req_id})")
                else:
                    self.logger.warning(f"No req_id found for {leg_name} at exit (index: {i})")
                    
        except Exception as e:
            self.logger.error(f"Error capturing exit Greeks: {e}")
    
    def get_position_value_with_greeks(self, trade: CalendarSpread) -> Tuple[Optional[float], dict]:
        """Get current market value of position AND return market data for Greeks capture"""
        try:
            # Create the 4 option contracts using actual executed strikes
            actual_long_put = trade.long_put_strike if trade.long_put_strike > 0 else trade.put_strike
            actual_long_call = trade.long_call_strike if trade.long_call_strike > 0 else trade.call_strike
            
            short_put = self.create_spxw_contract(trade.short_expiry, trade.put_strike, "P")
            short_call = self.create_spxw_contract(trade.short_expiry, trade.call_strike, "C")
            long_put = self.create_spxw_contract(trade.long_expiry, actual_long_put, "P")
            long_call = self.create_spxw_contract(trade.long_expiry, actual_long_call, "C")
            
            contracts = [short_put, short_call, long_put, long_call]
            market_data = {}
            
            # Request market data for all contracts with Greeks
            for i, contract in enumerate(contracts):
                req_id = self.get_next_req_id()
                self.client.reqMktData(req_id, contract, "106", False, False, [])  # Include Greeks
                market_data[i] = req_id
                time.sleep(0.1)
            
            # Wait for market data
            time.sleep(2)
            
            # Calculate current spread value (what we'd get if we closed now)
            spread_value = 0.0
            all_data_received = True
            
            for i, req_id in market_data.items():
                if req_id in self.wrapper.market_data:
                    data = self.wrapper.market_data[req_id]
                    if 'bid' in data and 'ask' in data and data['bid'] > 0 and data['ask'] > 0:
                        mid_price = (data['bid'] + data['ask']) / 2
                        # Double Calendar spread closing value:
                        if i < 2:  # Short legs (we sold these originally, now buy back)
                            spread_value -= mid_price  # We pay to buy back shorts
                        else:  # Long legs (we bought these originally, now sell)
                            spread_value += mid_price  # We receive for selling longs
                    else:
                        self.logger.warning(f"No bid/ask data for contract {i}")
                        all_data_received = False
                        break
                else:
                    self.logger.warning(f"No market data received for req_id {req_id}")
                    all_data_received = False
                    break
            
            if not all_data_received:
                # Cancel all requests and return None
                for req_id in market_data.values():
                    self.client.cancelMktData(req_id)
                return None, {}
            
            # Round to nearest nickel
            spread_value = round(spread_value * 20) / 20
            
            self.logger.info(f"Position value for {trade.trade_id}: Close value ${spread_value:.2f}")
            
            # Return both value and market data dict (don't cancel requests yet - Greeks capture needs them)
            return spread_value, market_data
            
        except Exception as e:
            self.logger.error(f"Error getting position value with Greeks: {e}")
            return None, {}
    
    def verify_contracts_exist(self, expiry_21: str, expiry_28: str, 
                             put_strike: float, call_strike: float) -> bool:
        """Verify all 4 required contracts exist"""
        contracts_to_check = [
            self.create_spxw_contract(expiry_21, put_strike, "P"),
            self.create_spxw_contract(expiry_21, call_strike, "C"),
            self.create_spxw_contract(expiry_28, put_strike, "P"),
            self.create_spxw_contract(expiry_28, call_strike, "C")
        ]
        
        for contract in contracts_to_check:
            req_id = self.get_next_req_id()
            self.wrapper.contract_details_received.clear()
            
            self.client.reqContractDetails(req_id, contract)
            
            # Wait for response
            if not self.wrapper.contract_details_received.wait(timeout=5):
                self.logger.error(f"Contract verification timeout: {contract.strike} {contract.right} {contract.lastTradeDateOrContractMonth}")
                return False
            
            if req_id not in self.wrapper.contract_details or not self.wrapper.contract_details[req_id]:
                self.logger.error(f"Contract not found: {contract.strike} {contract.right} {contract.lastTradeDateOrContractMonth}")
                return False
        
        return True
    
    def get_available_strikes(self, expiry: str) -> List[float]:
        """Get list of available strike prices for a given expiry"""
        # Create a generic SPX contract for this expiry
        spx_contract = Contract()
        spx_contract.symbol = "SPX"
        spx_contract.secType = "OPT"
        spx_contract.exchange = "SMART"
        spx_contract.currency = "USD"
        spx_contract.lastTradeDateOrContractMonth = expiry
        spx_contract.tradingClass = "SPXW"
        
        req_id = self.get_next_req_id()
        self.wrapper.contract_details_received.clear()
        self.client.reqContractDetails(req_id, spx_contract)
        
        # Wait for contract details
        if self.wrapper.contract_details_received.wait(timeout=15):
            if req_id in self.wrapper.contract_details:
                strikes = []
                for detail in self.wrapper.contract_details[req_id]:
                    strikes.append(detail.contract.strike)
                return sorted(list(set(strikes)))  # Remove duplicates and sort
        
        return []
    
    def find_nearest_available_strikes(self, expiry: str, target_put: float, target_call: float) -> Tuple[Optional[float], Optional[float]]:
        """Find closest available strikes to targets within deviation limits"""
        available_strikes = self.get_available_strikes(expiry)
        
        if not available_strikes:
            return None, None
        
        # Find nearest put strike (at or below target)
        put_candidates = [s for s in available_strikes if s <= target_put]
        nearest_put = max(put_candidates) if put_candidates else None
        
        # Find nearest call strike (at or above target)
        call_candidates = [s for s in available_strikes if s >= target_call]
        nearest_call = min(call_candidates) if call_candidates else None
        
        # Check deviation limits
        max_deviation = self.config.max_strike_deviation
        
        if nearest_put and abs(nearest_put - target_put) > max_deviation:
            self.logger.warning(f"Put strike deviation too large: {abs(nearest_put - target_put)} > {max_deviation}")
            nearest_put = None
            
        if nearest_call and abs(nearest_call - target_call) > max_deviation:
            self.logger.warning(f"Call strike deviation too large: {abs(nearest_call - target_call)} > {max_deviation}")
            nearest_call = None
        
        return nearest_put, nearest_call
    
    def handle_failed_trade(self, expiry_21: str, expiry_28: str, put_strike: float, call_strike: float, spx_price: float) -> Optional[Tuple[float, float, float, float]]:
        """Handle failed trade based on user settings. Returns (short_put, short_call, long_put, long_call) or None"""
        
        action = self.config.failed_trade_action
        
        if action == "skip":
            # Original behavior
            message = "Required option contracts do not exist"
            self.logger.error(message)
            self.db.log_daily_action("CONTRACTS_MISSING", message, False)
            self.notifications.notify_trade_failed(message,
                                                 spx_price=spx_price,
                                                 put_strike=put_strike,
                                                 call_strike=call_strike,
                                                 short_expiry=expiry_21,
                                                 long_expiry=expiry_28)
            return None
            
        elif action == "adjust_longs":
            # Keep original short strikes, find nearest available long strikes
            self.logger.info(f"Attempting to adjust long strikes only...")
            
            # Check if short strikes exist
            if not self.verify_contracts_exist(expiry_21, expiry_21, put_strike, call_strike):
                message = "Short option contracts do not exist - cannot adjust"
                self.logger.error(message)
                self.notifications.notify_trade_failed(message, spx_price=spx_price,
                                                     put_strike=put_strike, call_strike=call_strike,
                                                     short_expiry=expiry_21, long_expiry=expiry_28)
                return None
            
            # Find nearest available long strikes
            long_put, long_call = self.find_nearest_available_strikes(expiry_28, put_strike, call_strike)
            
            if long_put is None or long_call is None:
                message = f"No suitable long strikes found within {self.config.max_strike_deviation} points"
                self.logger.error(message)
                self.notifications.notify_trade_failed(message, spx_price=spx_price,
                                                     put_strike=put_strike, call_strike=call_strike,
                                                     short_expiry=expiry_21, long_expiry=expiry_28)
                return None
            
            # Verify the adjusted contracts exist
            if not self.verify_contracts_exist(expiry_28, expiry_28, long_put, long_call):
                message = "Adjusted long contracts verification failed"
                self.logger.error(message)
                self.notifications.notify_trade_failed(message, spx_price=spx_price,
                                                     put_strike=put_strike, call_strike=call_strike,
                                                     short_expiry=expiry_21, long_expiry=expiry_28)
                return None
            
            self.logger.info(f"Adjusted trade: Short {put_strike}P/{call_strike}C, Long {long_put}P/{long_call}C")
            return put_strike, call_strike, long_put, long_call
            
        elif action == "adjust_entire":
            # Find strikes that exist on both expiries
            self.logger.info(f"Attempting to adjust entire trade...")
            
            # Find nearest available strikes for the long expiry
            adjusted_put, adjusted_call = self.find_nearest_available_strikes(expiry_28, put_strike, call_strike)
            
            if adjusted_put is None or adjusted_call is None:
                message = f"No suitable strikes found within {self.config.max_strike_deviation} points"
                self.logger.error(message)
                self.notifications.notify_trade_failed(message, spx_price=spx_price,
                                                     put_strike=put_strike, call_strike=call_strike,
                                                     short_expiry=expiry_21, long_expiry=expiry_28)
                return None
            
            # Verify both expiries have the adjusted strikes
            if not self.verify_contracts_exist(expiry_21, expiry_21, adjusted_put, adjusted_call):
                message = "Adjusted short contracts do not exist"
                self.logger.error(message)
                self.notifications.notify_trade_failed(message, spx_price=spx_price,
                                                     put_strike=adjusted_put, call_strike=adjusted_call,
                                                     short_expiry=expiry_21, long_expiry=expiry_28)
                return None
                
            if not self.verify_contracts_exist(expiry_28, expiry_28, adjusted_put, adjusted_call):
                message = "Adjusted long contracts do not exist"
                self.logger.error(message)
                self.notifications.notify_trade_failed(message, spx_price=spx_price,
                                                     put_strike=adjusted_put, call_strike=adjusted_call,
                                                     short_expiry=expiry_21, long_expiry=expiry_28)
                return None
            
            self.logger.info(f"Adjusted trade: {adjusted_put}P/{adjusted_call}C on both expiries")
            return adjusted_put, adjusted_call, adjusted_put, adjusted_call
        
        # Fallback to skip
        return None
    
    def find_delta_strikes(self, expiry: str, spx_price: float) -> Tuple[Optional[float], Optional[float]]:
        """Find strikes closest to 20D for puts and calls using REAL delta calculations from IBKR"""
        self.logger.info(f"Finding REAL 20D strikes for {expiry}...")
        
        # Get options chain from IBKR
        self.logger.info("Requesting options chain from IBKR...")
        
        # Create SPX contract for options chain request
        spx_contract = Contract()
        spx_contract.symbol = "SPX"
        spx_contract.secType = "OPT"
        spx_contract.exchange = "SMART"
        spx_contract.currency = "USD"
        spx_contract.lastTradeDateOrContractMonth = expiry
        spx_contract.tradingClass = "SPXW"
        
        # Request contract details to get available strikes
        req_id = self.req_id_counter
        self.req_id_counter += 1
        
        self.wrapper.contract_details_received.clear()
        self.client.reqContractDetails(req_id, spx_contract)
        
        # Wait for contract details
        if self.wrapper.contract_details_received.wait(timeout=15):
            self.logger.info("Options chain received")
        else:
            self.logger.error("Timeout waiting for options chain")
            return None, None
        
        # Extract available strikes and get their deltas
        if req_id in self.wrapper.contract_details:
            available_strikes = []
            for contract_detail in self.wrapper.contract_details[req_id]:
                strike = contract_detail.contract.strike
                if strike > 0:
                    available_strikes.append(strike)
            
            available_strikes = sorted(set(available_strikes))
            self.logger.info(f"Found {len(available_strikes)} available strikes")
            
            # Focus on strikes likely to be around 20D based on SPX price
            # For 20D: puts typically 200-300 points below SPX, calls 100-200 points above SPX
            put_candidates = [s for s in available_strikes 
                            if s >= spx_price - 350 and s <= spx_price - 150]  # Puts below SPX
            call_candidates = [s for s in available_strikes 
                             if s >= spx_price + 50 and s <= spx_price + 250]   # Calls above SPX
            
            candidate_strikes = sorted(set(put_candidates + call_candidates))
            
            self.logger.info(f"Checking deltas for {len(candidate_strikes)} candidate strikes...")
            
            delta_data = {}
            
            # Request market data with greeks for each candidate strike
            for strike in candidate_strikes:
                # Check both puts and calls for this strike
                for right in ['P', 'C']:
                    contract = self.create_spxw_contract(expiry, strike, right)
                    
                    req_id = self.req_id_counter
                    self.req_id_counter += 1
                    
                    # Request market data with greeks
                    self.client.reqMktData(req_id, contract, "", False, False, [])
                    
                    # Store mapping
                    delta_data[req_id] = {'strike': strike, 'right': right}
            
            # Wait for delta data
            self.logger.info("Waiting for delta calculations...")
            time.sleep(10)  # Give time for delta calculations
            
            # Find the strikes closest to 20D
            best_put = None
            best_call = None
            best_put_delta_diff = float('inf')
            best_call_delta_diff = float('inf')
            
            for req_id, info in delta_data.items():
                if req_id in self.wrapper.market_data and 'delta' in self.wrapper.market_data[req_id]:
                    delta = self.wrapper.market_data[req_id]['delta']
                    strike = info['strike']
                    right = info['right']
                    
                    if right == 'P' and delta is not None:
                        # Put delta is negative, we want -0.20
                        delta_diff = abs(abs(delta) - self.config.target_delta)
                        if delta_diff < best_put_delta_diff:
                            best_put_delta_diff = delta_diff
                            best_put = {'strike': strike, 'delta': delta}
                            
                    elif right == 'C' and delta is not None:
                        # Call delta is positive, we want +0.20
                        delta_diff = abs(delta - self.config.target_delta)
                        if delta_diff < best_call_delta_diff:
                            best_call_delta_diff = delta_diff
                            best_call = {'strike': strike, 'delta': delta}
                    
                    self.logger.info(f"{strike}{right}: Delta={delta:.3f}")
                
                # Cancel market data request
                self.client.cancelMktData(req_id)
            
            if best_put and best_call:
                self.logger.info(f"Best 20D Put: {best_put['strike']} (Delta={best_put['delta']:.3f})")
                self.logger.info(f"Best 20D Call: {best_call['strike']} (Delta={best_call['delta']:.3f})")
                
                return best_put['strike'], best_call['strike']
            else:
                self.logger.error("Could not find suitable 20D strikes")
                return None, None
        else:
            self.logger.error("No contract details received")
            return None, None
    
    # get_option_delta method removed - now using comprehensive delta search in find_delta_strikes
    
    def start_scheduler(self):
        """Start the daily trading scheduler"""
        # Schedule daily execution at 9:45 AM ET on weekdays
        schedule.every().monday.at("09:45").do(self.daily_trading_routine)
        schedule.every().tuesday.at("09:45").do(self.daily_trading_routine)
        schedule.every().wednesday.at("09:45").do(self.daily_trading_routine)
        schedule.every().thursday.at("09:45").do(self.daily_trading_routine)
        schedule.every().friday.at("09:45").do(self.daily_trading_routine)
        
        # Schedule daily exit check at 3:00 PM ET on weekdays
        schedule.every().monday.at("15:00").do(self.daily_exit_check)
        schedule.every().tuesday.at("15:00").do(self.daily_exit_check)
        schedule.every().wednesday.at("15:00").do(self.daily_exit_check)
        schedule.every().thursday.at("15:00").do(self.daily_exit_check)
        schedule.every().friday.at("15:00").do(self.daily_exit_check)
        
        # Schedule daily position reconciliation at 5:00 PM ET on weekdays
        schedule.every().monday.at("17:00").do(self.daily_position_reconciliation)
        schedule.every().tuesday.at("17:00").do(self.daily_position_reconciliation)
        schedule.every().wednesday.at("17:00").do(self.daily_position_reconciliation)
        schedule.every().thursday.at("17:00").do(self.daily_position_reconciliation)
        schedule.every().friday.at("17:00").do(self.daily_position_reconciliation)
        
        print("✓ Trading scheduler started")
        print("Scheduled: Daily execution at 9:45 AM ET (M-F)")
        print("Scheduled: Daily exit check at 3:00 PM ET (M-F)")
        print("Scheduled: Daily position reconciliation at 5:00 PM ET (M-F)")
        
        # Connect to IBKR for scheduled operations
        print("🔌 Connecting to IBKR for scheduled operations...")
        if self.connect_to_ibkr():
            print("✅ IBKR connection established")
        else:
            print("⚠️ IBKR connection failed - will retry when needed")
        
        # Run scheduler loop
        while True:
            schedule.run_pending()
            self.process_web_commands()  # Process commands from web interface
            time.sleep(30)  # Check every 30 seconds
    
    def daily_trading_routine(self):
        """Main daily trading routine executed at 9:45 AM"""
        try:
            current_time = self.get_local_time()
            self.logger.warning(f"DAILY TRADING ROUTINE STARTED at {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            
            # Safety check: Only execute during reasonable hours (9:30-10:00 AM ET)
            if current_time.hour != 9 or current_time.minute < 30:
                if not (current_time.hour == 10 and current_time.minute == 0):  # Allow 10:00 AM too
                    error_msg = f"SAFETY ABORT: Daily routine triggered at wrong time: {current_time.strftime('%H:%M')} (should be 9:45 AM)"
                    self.logger.error(error_msg)
                    self.notifications.notify_trade_failed(error_msg)
                    return
            
            self.logger.info("Starting daily SPX calendar spread routine")
            
            # Connect to IBKR if not connected
            if not self.is_connected:
                if not self.connect_to_ibkr():
                    self.db.log_daily_action("CONNECTION_FAILED", "Could not connect to IBKR", False)
                    self.notifications.notify_trade_failed("Could not connect to IBKR")
                    return
            
            # Check position limits
            active_trades = self.db.get_active_trades()
            if len(active_trades) >= self.config.max_concurrent_positions:
                message = f"Maximum positions reached: {len(active_trades)}/{self.config.max_concurrent_positions}"
                self.logger.info(message)
                self.db.log_daily_action("POSITION_LIMIT", message, True)
                self.notifications.notify_trade_failed(message)
                return
            
            # Check if we already traded today (use Eastern time)
            today = self.get_local_time().strftime('%Y-%m-%d')
            if self.db.get_trade_count_for_date(today) > 0:
                message = "Already traded today"
                self.logger.info(message)
                self.db.log_daily_action("ALREADY_TRADED", message, True)
                return
            
            # Execute the trading logic (forced execution for scheduled routine)
            self.execute_calendar_spread_entry(force_execution=True)
            
        except Exception as e:
            error_msg = f"Daily trading routine error: {e}"
            self.logger.error(error_msg)
            self.db.log_daily_action("ROUTINE_ERROR", error_msg, False)
            self.notifications.notify_trade_failed(error_msg)
    
    def execute_calendar_spread_entry(self, force_execution=False, is_manual=False):
        """Execute the calendar spread entry logic"""
        import traceback
        import inspect
        
        try:
            # CRITICAL: Log who called this function and when
            current_time = self.get_local_time()
            caller_frame = inspect.currentframe().f_back
            caller_info = f"{caller_frame.f_code.co_filename}:{caller_frame.f_lineno} in {caller_frame.f_code.co_name}"
            
            trade_type = "MANUAL" if is_manual else "SCHEDULED" if force_execution else "UNKNOWN"
            self.logger.warning(f"{trade_type} TRADE EXECUTION TRIGGERED at {current_time.strftime('%Y-%m-%d %H:%M:%S')} by {caller_info}")
            self.logger.warning(f"Force execution: {force_execution}, Manual: {is_manual}")
            self.logger.warning(f"Call stack: {''.join(traceback.format_stack())}")
            
            # Step 1: Get current SPX price
            self.current_spx_price = self.get_spx_price()
            self.logger.info(f"Current SPX price: {self.current_spx_price:.2f}")
            
            # Step 2: Calculate expiry dates
            expiry_21, expiry_28 = self.calculate_expiry_dates()
            self.logger.info(f"Target expiries: {expiry_21} (21d), {expiry_28} (28d)")
            
            # Step 3: Find delta-based strikes
            put_strike, call_strike = self.find_delta_strikes(expiry_21, self.current_spx_price)
            
            if put_strike is None or call_strike is None:
                message = "Could not find suitable delta strikes"
                self.logger.error(message)
                self.db.log_daily_action("NO_STRIKES", message, False)
                self.notifications.notify_trade_failed(message, 
                                                     spx_price=self.current_spx_price,
                                                     short_expiry=expiry_21, 
                                                     long_expiry=expiry_28)
                return
            
            self.logger.info(f"Selected strikes: {put_strike}P / {call_strike}C")
            
            # Step 3.5: Check for ghost strikes and handle them
            ghost_action = self.db.get_setting('ghost_strike_action', 'move')
            has_conflict, conflict_desc = self.check_ghost_strikes(put_strike, call_strike)
            
            if has_conflict:
                self.logger.warning(f"Ghost strike detected: {conflict_desc}")
                
                if ghost_action == 'skip':
                    message = f"Trade skipped due to ghost strike conflict: {conflict_desc}"
                    self.logger.warning(message)
                    self.db.log_daily_action("GHOST_STRIKE_SKIP", message, False)
                    self.notifications.notify_trade_failed(message, 
                                                         spx_price=self.current_spx_price,
                                                         short_expiry=expiry_21, 
                                                         long_expiry=expiry_28)
                    return
                elif ghost_action == 'move':
                    original_put = put_strike
                    original_call = call_strike
                    
                    # Check which strikes need adjustment
                    active_trades = self.db.get_active_trades()
                    last_week_trade = None
                    current_date = self.get_local_time().date()
                    
                    for trade in active_trades:
                        trade_date = datetime.strptime(trade.entry_date, '%Y-%m-%d').date()
                        days_ago = (current_date - trade_date).days
                        if 6 <= days_ago <= 8:
                            last_week_trade = trade
                            break
                    
                    if last_week_trade:
                        # Adjust conflicting strikes
                        last_week_long_put = last_week_trade.long_put_strike if last_week_trade.long_put_strike != 0.0 else last_week_trade.put_strike
                        last_week_long_call = last_week_trade.long_call_strike if last_week_trade.long_call_strike != 0.0 else last_week_trade.call_strike
                        
                        if put_strike == last_week_long_put:
                            put_strike = self.adjust_ghost_strike(put_strike, 'P', expiry_21)
                            self.logger.info(f"Adjusted put strike from {original_put} to {put_strike}")
                        
                        if call_strike == last_week_long_call:
                            call_strike = self.adjust_ghost_strike(call_strike, 'C', expiry_21)
                            self.logger.info(f"Adjusted call strike from {original_call} to {call_strike}")
                        
                        # Log the adjustment
                        adjustment_msg = f"Ghost strike adjustment: {original_put}P/{original_call}C -> {put_strike}P/{call_strike}C"
                        self.logger.info(adjustment_msg)
                        self.db.log_daily_action("GHOST_STRIKE_ADJUST", adjustment_msg, True)
                elif ghost_action == 'ignore':
                    self.logger.warning(f"Proceeding with ghost strike (user setting): {conflict_desc}")
                    self.db.log_daily_action("GHOST_STRIKE_IGNORE", f"Ghost strike ignored: {conflict_desc}", True)
            
            # Step 4: Verify contracts exist or handle adjustments
            if not self.verify_contracts_exist(expiry_21, expiry_28, put_strike, call_strike):
                self.logger.info("Original contracts not found, checking adjustment options...")
                
                # Try to handle the failed trade based on user settings
                adjusted_strikes = self.handle_failed_trade(expiry_21, expiry_28, put_strike, call_strike, self.current_spx_price)
                
                if adjusted_strikes is None:
                    # No adjustment possible or user chose to skip
                    return
                
                # Unpack adjusted strikes
                short_put_strike, short_call_strike, long_put_strike, long_call_strike = adjusted_strikes
                
                # Update strikes for execution
                put_strike = short_put_strike
                call_strike = short_call_strike
                
                # Log the adjustment
                if short_put_strike != long_put_strike or short_call_strike != long_call_strike:
                    adjustment_msg = f"Trade adjusted: Short {short_put_strike}P/{short_call_strike}C, Long {long_put_strike}P/{long_call_strike}C"
                else:
                    adjustment_msg = f"Trade adjusted: {short_put_strike}P/{short_call_strike}C on both expiries"
                
                self.logger.info(adjustment_msg)
                self.db.log_daily_action("TRADE_ADJUSTED", adjustment_msg, True)
                
                # Store adjusted long strikes for order creation
                adjusted_long_put = long_put_strike
                adjusted_long_call = long_call_strike
            else:
                # Original contracts exist, no adjustment needed
                adjusted_long_put = put_strike
                adjusted_long_call = call_strike
            
            # Step 5: Create trade record
            local_time = self.get_local_time()
            trade_id = f"CAL_{local_time.strftime('%Y%m%d_%H%M%S')}"
            trade = CalendarSpread(
                trade_id=trade_id,
                entry_date=local_time.strftime('%Y-%m-%d'),
                entry_time=local_time.strftime('%H:%M:%S'),
                spx_price=self.current_spx_price,
                short_expiry=expiry_21,
                long_expiry=expiry_28,
                put_strike=put_strike,        # Short strikes
                call_strike=call_strike,      # Short strikes
                long_put_strike=adjusted_long_put,    # Long strikes (may be adjusted)
                long_call_strike=adjusted_long_call   # Long strikes (may be adjusted)
            )
            
            # Step 6: Place the order first to get starting bid, then notify
            # Step 7: Place the order (with potentially adjusted long strikes)
            if self.place_calendar_spread_order(trade, adjusted_long_put, adjusted_long_call):
                # Update trade record with actual executed strikes if adjusted
                if adjusted_long_put != put_strike or adjusted_long_call != call_strike:
                    # For "Adjust Longs Only", we need to store the actual long strikes somewhere
                    # Since the CalendarSpread dataclass only has put_strike/call_strike (for shorts),
                    # we'll add a note to the database that this trade has asymmetric strikes
                    self.logger.info(f"Trade has asymmetric strikes - Short: {put_strike}P/{call_strike}C, Long: {adjusted_long_put}P/{adjusted_long_call}C")
                    
                    # For now, let's add the adjustment info to the trade status or create a custom field
                    # We'll store the long strikes in a special way for streaming to work
                
                self.db.save_trade(trade)
                self.db.log_daily_action("TRADE_PLACED", f"Calendar spread placed: {trade_id}", True)
                self.logger.info(f"Trade placed successfully: {trade_id}")
                
                # Send success notification with fill price
                if adjusted_long_put != put_strike or adjusted_long_call != call_strike:
                    success_msg = f"SPX Calendar FILLED (ADJUSTED): ${trade.entry_credit:.2f} debit. Target: ${trade.profit_target:.2f}. Short:{put_strike}P/{call_strike}C Long:{adjusted_long_put}P/{adjusted_long_call}C"
                else:
                    success_msg = f"SPX Calendar FILLED: ${trade.entry_credit:.2f} debit. Target: ${trade.profit_target:.2f}. Strikes: {put_strike}P/{call_strike}C"
                
                self.logger.info(f"Sending success SMS: {success_msg}")
                self.notifications.send_sms(success_msg)
            else:
                self.db.log_daily_action("ORDER_FAILED", f"Failed to place order: {trade_id}", False)
                self.logger.error(f"Failed to place trade: {trade_id}")
            
        except Exception as e:
            error_msg = f"Calendar spread entry error: {e}"
            self.logger.error(error_msg)
            self.db.log_daily_action("ENTRY_ERROR", error_msg, False)
            self.notifications.notify_trade_failed(error_msg)
    
    def place_calendar_spread_order(self, trade: CalendarSpread, long_put_strike: float = None, long_call_strike: float = None) -> bool:
        """Place the 4-leg calendar spread order with price improvement logic"""
        try:
            # Use adjusted strikes if provided, otherwise use trade's strikes
            actual_long_put = long_put_strike if long_put_strike is not None else trade.put_strike
            actual_long_call = long_call_strike if long_call_strike is not None else trade.call_strike
            
            # Create the 4 option contracts and get their contract details
            contracts = []
            contract_specs = [
                (trade.short_expiry, trade.put_strike, "P"),
                (trade.short_expiry, trade.call_strike, "C"),
                (trade.long_expiry, actual_long_put, "P"),
                (trade.long_expiry, actual_long_call, "C")
            ]
            
            # Get contract details for each contract to populate conId
            for expiry, strike, right in contract_specs:
                contract = self.create_spxw_contract(expiry, strike, right)
                
                # Request contract details to get the conId
                req_id = self.get_next_req_id()
                self.wrapper.contract_details_received.clear()
                self.client.reqContractDetails(req_id, contract)
                
                # Wait for contract details
                if self.wrapper.contract_details_received.wait(timeout=5):
                    if req_id in self.wrapper.contract_details:
                        contract_detail = self.wrapper.contract_details[req_id][0]
                        contract.conId = contract_detail.contract.conId
                        contracts.append(contract)
                        self.logger.info(f"Contract details received for {strike}{right} {expiry}: conId={contract.conId}")
                    else:
                        self.logger.error(f"No contract details found for {strike}{right} {expiry}")
                        return False
                else:
                    self.logger.error(f"Timeout getting contract details for {strike}{right} {expiry}")
                    return False
                
                time.sleep(0.1)  # Small delay between requests
            market_data = {}
            
            for i, contract in enumerate(contracts):
                req_id = self.get_next_req_id()
                self.client.reqMktData(req_id, contract, "106", False, False, [])  # Include Greeks (tick type 106)
                market_data[i] = req_id
                time.sleep(0.1)  # Small delay between requests
            
            # Wait for market data
            time.sleep(2)
            
            # Calculate spread mid price
            spread_mid = 0.0
            all_data_received = True
            
            for i, req_id in market_data.items():
                if (req_id in self.wrapper.market_data and 
                    'bid' in self.wrapper.market_data[req_id] and 
                    'ask' in self.wrapper.market_data[req_id]):
                    
                    bid = self.wrapper.market_data[req_id]['bid']
                    ask = self.wrapper.market_data[req_id]['ask']
                    mid = (bid + ask) / 2
                    
                                # Double Calendar spread: We pay a debit (long-term more expensive)
                    if i < 2:  # Short legs (we sell these - receive premium)
                        spread_mid -= mid  # Subtract what we receive
                    else:  # Long legs (we buy these - pay premium)
                        spread_mid += mid  # Add what we pay
                    
                    self.client.cancelMktData(req_id)
                else:
                    all_data_received = False
                    break
            
            if not all_data_received:
                self.logger.error("Could not get market data for all contracts")
                return False
            
            # Round to nearest 0.05 (SPX option minimum tick)
            spread_mid = round(spread_mid * 20) / 20
            trade.last_bid_price = spread_mid
            
            self.logger.info(f"Calculated spread mid price: ${spread_mid:.2f}")
            
            # Send initial notification with starting bid
            self.notifications.notify_trade_attempt(
                self.current_spx_price, trade.put_strike, trade.call_strike, 
                trade.short_expiry, trade.long_expiry, spread_mid
            )
            
            # Attempt to fill the order with price improvement logic
            # Start at mid price, then increase bid if not filled
            current_bid = spread_mid
            max_bid = spread_mid + self.config.max_spread_premium  # Cap maximum bid
            
            self.logger.info(f"💰 Pricing strategy: Start ${current_bid:.2f}, Max ${max_bid:.2f} (premium cap: ${self.config.max_spread_premium:.2f})")
            
            for attempt in range(self.config.max_price_attempts):
                trade.fill_attempts = attempt + 1
                
                # Create combo order
                if self.wrapper.next_order_id is None:
                    self.logger.error("No valid order ID available")
                    return False
                
                order_id = self.wrapper.next_order_id
                self.wrapper.next_order_id += 1  # CRITICAL FIX: Increment for next attempt
                
                combo_order = self.create_combo_order(current_bid, self.config.position_size)
                combo_contract = self.create_combo_contract(contracts)
                
                # Place the order
                self.client.placeOrder(order_id, combo_contract, combo_order)
                trade.combo_order_id = order_id
                
                self.logger.info(f"📤 Entry order placed (attempt {attempt + 1}/{self.config.max_price_attempts}): ID {order_id}, Price ${current_bid:.2f}")
                
                # Wait for fill
                fill_timeout = self.config.fill_wait_time
                start_time = time.time()
                
                while time.time() - start_time < fill_timeout:
                    if (order_id in self.wrapper.orders and 
                        self.wrapper.orders[order_id]['status'] == 'Filled'):
                        
                        # Order filled successfully
                        fill_price = self.wrapper.orders[order_id]['avg_fill_price']
                        trade.entry_credit = fill_price  # This is actually a debit (positive cost)
                        # For debit spreads, profit target is entry_debit * (1 + profit_target_pct)
                        profit_target = fill_price * (1 + self.config.profit_target_pct)
                        trade.profit_target = round(profit_target * 20) / 20  # Round to nearest 0.05
                        trade.status = "ACTIVE"
                        trade.fill_status = "FILLED"
                        
                        # Capture Greeks and IV at entry
                        self.logger.info("Capturing entry Greeks and IV...")
                        self._capture_entry_greeks(trade, market_data)
                        
                        # Place profit target order (GTC)
                        self.logger.info("Placing profit target order...")
                        profit_target_success = self.place_profit_target_order(trade)
                        if not profit_target_success:
                            self.logger.warning(f"Failed to place profit target order for {trade.trade_id}")
                            self.notifications.send_sms(f"SPX Calendar: Entry filled but profit target order failed for {trade.trade_id}. Check logs for details.")
                        else:
                            self.logger.info("[OK] Profit target GTC order placed successfully")
                        
                        # Start streaming for the new position
                        try:
                            self.logger.info(f"Starting streaming for new position {trade.trade_id}...")
                            self.start_position_streaming(trade)
                            self.logger.info("[OK] Streaming started for new position")
                        except Exception as stream_error:
                            self.logger.warning(f"Failed to start streaming for {trade.trade_id}: {stream_error}")
                            # Don't fail the whole trade for streaming issues
                        
                        self.logger.info(f"[OK] Order filled: ${fill_price:.2f}")
                        # Success notification will be sent by calling function
                        return True
                    
                    time.sleep(1)
                
                # Order not filled, cancel and try higher price
                self.logger.info(f"⏰ Order {order_id} not filled after {fill_timeout}s - cancelling and trying next attempt")
                self.client.cancelOrder(order_id)
                time.sleep(1)  # Wait for cancellation
                
                if attempt < self.config.max_price_attempts - 1:
                    next_bid = current_bid + self.config.price_increment
                    next_bid = round(next_bid * 20) / 20  # Round to 0.05 (SPX tick)
                    
                    # Don't exceed maximum bid
                    if next_bid <= max_bid:
                        current_bid = next_bid
                        trade.last_bid_price = current_bid
                        self.logger.info(f"💰 Increasing bid to ${current_bid:.2f} for attempt {attempt + 2}")
                    else:
                        self.logger.warning(f"[WARNING] Next bid ${next_bid:.2f} would exceed max ${max_bid:.2f} - stopping attempts")
                        break
            
            # All attempts failed
            trade.status = "CANCELLED"
            trade.fill_status = "UNFILLED"
            message = f"❌ Entry order failed: Not filled after {self.config.max_price_attempts} attempts (${trade.last_bid_price:.2f} final bid)"
            self.logger.error(message)
            
            # Send SMS with simplified failure message
            failure_sms = f"SPX Calendar FAILED: Not filled after {self.config.max_price_attempts} attempts. Final bid: ${trade.last_bid_price:.2f}"
            self.notifications.send_sms(failure_sms)
            return False
            
        except Exception as e:
            error_msg = f"Order placement error: {e}"
            self.logger.error(error_msg)
            trade.status = "CANCELLED"
            return False
    
    def create_combo_order(self, price: float, quantity: int) -> Order:
        """Create combo order for the 4-leg spread"""
        order = Order()
        order.action = "BUY"  # Net position is buying the spread
        order.orderType = "LMT"
        order.totalQuantity = quantity
        order.lmtPrice = price
        order.transmit = True
        
        # Clear any potentially problematic attributes
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.outsideRth = False
        
        return order
    
    def create_combo_contract(self, contracts: List[Contract]) -> Contract:
        """Create combo contract for the 4-leg spread"""
        combo = Contract()
        combo.symbol = "SPX"
        combo.secType = "BAG"
        combo.exchange = "SMART"
        combo.currency = "USD"
        
        # Define the legs
        from ibapi.contract import ComboLeg
        combo.comboLegs = []
        
        # contracts = [short_put, short_call, long_put, long_call]
        
        # Short put (sell) - contracts[0]
        leg1 = ComboLeg()
        leg1.conId = contracts[0].conId if contracts[0].conId else 0
        leg1.ratio = 1
        leg1.action = "SELL"
        leg1.exchange = "SMART"
        combo.comboLegs.append(leg1)
        
        # Short call (sell) - contracts[1]
        leg2 = ComboLeg()
        leg2.conId = contracts[1].conId if contracts[1].conId else 0
        leg2.ratio = 1
        leg2.action = "SELL"
        leg2.exchange = "SMART"
        combo.comboLegs.append(leg2)
        
        # Long put (buy) - contracts[2]
        leg3 = ComboLeg()
        leg3.conId = contracts[2].conId if contracts[2].conId else 0
        leg3.ratio = 1
        leg3.action = "BUY"
        leg3.exchange = "SMART"
        combo.comboLegs.append(leg3)
        
        # Long call (buy) - contracts[3]
        leg4 = ComboLeg()
        leg4.conId = contracts[3].conId if contracts[3].conId else 0
        leg4.ratio = 1
        leg4.action = "BUY"
        leg4.exchange = "SMART"
        combo.comboLegs.append(leg4)
        
        return combo
    
    def daily_exit_check(self):
        """Daily 3:00 PM check: Close positions that should exit today"""
        try:
            self.logger.info("🕒 Daily exit check at 3:00 PM")
            
            if not self.client.isConnected():
                self.logger.warning("Not connected to IBKR - skipping exit check")
                return
            
            active_trades = self.db.get_active_trades()
            if not active_trades:
                self.logger.info("No active trades to check for exit")
                return
            
            current_date = self.get_local_time().date()
            exit_count = 0
            
            for trade in active_trades:
                if trade.status != "ACTIVE":
                    continue
                
                # Calculate days since entry
                entry_date = datetime.strptime(trade.entry_date, '%Y-%m-%d').date()
                days_since_entry = (current_date - entry_date).days
                
                # Check if this trade should exit today (14th day)
                if days_since_entry >= self.config.exit_day:
                    self.logger.info(f"🕒 Time exit triggered for {trade.trade_id} (day {days_since_entry})")
                    success = self.close_calendar_position(trade, f"Time exit - day {days_since_entry}")
                    if success:
                        exit_count += 1
                        # Send notification
                        self.notifications.send_sms(
                            f"SPX Calendar: Time exit for {trade.trade_id} on day {days_since_entry}. "
                            f"Position closed at 3:00 PM as scheduled."
                        )
            
            if exit_count > 0:
                self.logger.info(f"[OK] Daily exit check complete: {exit_count} positions closed")
                # Log the daily action
                self.db.log_daily_action(
                    'TIME_EXIT_CHECK',
                    f'Daily 3PM exit check: {exit_count} positions closed',
                    True
                )
            else:
                self.logger.info("[OK] Daily exit check complete: No positions needed closing")
            
        except Exception as e:
            self.logger.error(f"Daily exit check error: {e}")
            # Log the error
            self.db.log_daily_action(
                'TIME_EXIT_CHECK',
                f'Daily 3PM exit check failed: {str(e)}',
                False
            )
    
    def daily_position_reconciliation(self):
        """Daily 5:00 PM check: Reconcile system positions with IBKR reality"""
        try:
            self.logger.info("Daily position reconciliation at 5:00 PM")
            
            # Check if client exists (connection status check is unreliable)
            if not self.client:
                self.logger.error("IBKR client is None - skipping reconciliation")
                return
            
            # Note: client.isConnected() is unreliable with IBKR API, but if we have a client
            # and market data is streaming (which it clearly is), we can proceed
            self.logger.info("Proceeding with reconciliation (client exists and market data active)")
            
            # Get system positions
            active_trades = self.db.get_active_trades()
            if not active_trades:
                self.logger.info("No active positions to reconcile")
                self.db.log_daily_action('RECONCILIATION', 'No active positions to reconcile', True)
                return
            
            # Request IBKR positions
            self.logger.info("Requesting positions from IBKR for reconciliation...")
            
            # Try to ensure connection is active for position request
            try:
                # Test connection with a simple request first
                self.client.reqCurrentTime()
                time.sleep(1)  # Give it a moment
            except Exception as e:
                self.logger.warning(f"Connection test failed, attempting reconnection: {e}")
                if not self.reconnect_to_ibkr():
                    self.logger.error("Failed to reconnect for position reconciliation")
                    self.db.log_daily_action('RECONCILIATION', 'Failed to reconnect to IBKR', False)
                    return
            
            # Clear wrapper position data
            self.wrapper.positions = {}
            positions_received = {}
            
            # Create a temporary event for position completion
            import threading
            positions_complete = threading.Event()
            
            # Store original position method
            original_position = self.wrapper.position
            
            def temp_position(account, contract, position, avgCost):
                # Call original method first
                original_position(account, contract, position, avgCost)
                
                # Also collect for reconciliation
                if contract.symbol in ['SPX', 'SPXW'] and contract.secType == 'OPT':
                    key = f"{contract.symbol}-{contract.lastTradeDateOrContractMonth}-{contract.strike}-{contract.right}"
                    positions_received[key] = {
                        'symbol': contract.symbol,
                        'expiry': contract.lastTradeDateOrContractMonth,
                        'strike': float(contract.strike),
                        'right': contract.right,
                        'position': position,
                        'avg_cost': avgCost
                    }
            
            # Add positionEnd method temporarily
            def temp_position_end():
                positions_complete.set()
            
            # Override methods temporarily
            self.wrapper.position = temp_position
            self.wrapper.positionEnd = temp_position_end
            
            try:
                # Request positions
                self.client.reqPositions()
                
                # Wait for completion
                if positions_complete.wait(timeout=30):
                    self.logger.info(f"[OK] Received {len(positions_received)} SPX/SPXW positions from IBKR")
                    
                    # Analyze discrepancies
                    discrepancies = []
                    total_missing = 0
                    total_orphaned = 0
                    
                    # Check each system position
                    for trade in active_trades:
                        trade_discrepancies = []
                        
                        # Expected positions (IBKR reports as SPX regardless of SPX/SPXW)
                        expected_legs = [
                            (f"SPX-{trade.short_expiry}-{trade.put_strike}-P", -self.config.position_size, "Short Put"),
                            (f"SPX-{trade.short_expiry}-{trade.call_strike}-C", -self.config.position_size, "Short Call"),
                            (f"SPX-{trade.long_expiry}-{trade.long_put_strike if trade.long_put_strike else trade.put_strike}-P", self.config.position_size, "Long Put"),
                            (f"SPX-{trade.long_expiry}-{trade.long_call_strike if trade.long_call_strike else trade.call_strike}-C", self.config.position_size, "Long Call")
                        ]
                        
                        for key, expected_size, leg_name in expected_legs:
                            if key in positions_received:
                                actual_size = positions_received[key]['position']
                                if abs(actual_size - expected_size) > 0.1:
                                    trade_discrepancies.append(f"{leg_name}: Expected {expected_size}, Got {actual_size}")
                            else:
                                trade_discrepancies.append(f"{leg_name}: Missing from IBKR")
                                total_missing += 1
                        
                        if trade_discrepancies:
                            discrepancies.append(f"{trade.trade_id}: {', '.join(trade_discrepancies)}")
                    
                    # Check for orphaned positions
                    system_keys = set()
                    for trade in active_trades:
                        system_keys.update([
                            f"SPX-{trade.short_expiry}-{trade.put_strike}-P",
                            f"SPX-{trade.short_expiry}-{trade.call_strike}-C",
                            f"SPX-{trade.long_expiry}-{trade.long_put_strike if trade.long_put_strike else trade.put_strike}-P",
                            f"SPX-{trade.long_expiry}-{trade.long_call_strike if trade.long_call_strike else trade.call_strike}-C"
                        ])
                    
                    orphaned = []
                    for key, pos_data in positions_received.items():
                        if key not in system_keys and abs(pos_data['position']) > 0.1:
                            orphaned.append(f"{key}: {pos_data['position']} contracts")
                            total_orphaned += 1
                    
                    # Generate report
                    if discrepancies or orphaned:
                        self.logger.warning(f"[WARNING] Position discrepancies found:")
                        if discrepancies:
                            self.logger.warning(f"   System position issues: {len(discrepancies)}")
                            for disc in discrepancies[:5]:  # Show first 5
                                self.logger.warning(f"     - {disc}")
                        if orphaned:
                            self.logger.warning(f"   Orphaned IBKR positions: {len(orphaned)}")
                            for orph in orphaned[:5]:  # Show first 5
                                self.logger.warning(f"     - {orph}")
                        
                        # Send SMS for significant discrepancies
                        if total_missing > 4 or total_orphaned > 4:
                            self.notifications.send_sms(
                                f"SPX Calendar: Position reconciliation found {len(discrepancies)} position issues "
                                f"and {len(orphaned)} orphaned positions. Check logs for details."
                            )
                        
                        self.db.log_daily_action(
                            'RECONCILIATION',
                            f'Discrepancies found: {len(discrepancies)} position issues, {len(orphaned)} orphaned',
                            False
                        )
                    else:
                        self.logger.info("[OK] Position reconciliation: All positions match")
                        self.db.log_daily_action('RECONCILIATION', 'All positions reconciled successfully', True)
                
                else:
                    self.logger.error("[ERROR] IBKR position request timeout during reconciliation")
                    self.db.log_daily_action('RECONCILIATION', 'Position request timeout', False)
            
            finally:
                # Restore original method
                self.wrapper.position = original_position
                # Remove the temporary positionEnd method
                if hasattr(self.wrapper, 'positionEnd'):
                    delattr(self.wrapper, 'positionEnd')
            
        except Exception as e:
            self.logger.error(f"Position reconciliation error: {e}")
            self.db.log_daily_action('RECONCILIATION', f'Reconciliation failed: {str(e)}', False)
    
    def process_web_commands(self):
        """Process pending commands from the web interface"""
        try:
            commands = self.db.get_pending_commands()
            
            for command in commands:
                command_id, command_type, trade_id, parameters, created_at = command
                
                self.logger.info(f"Processing web command: {command_type} for trade {trade_id}")
                
                # Mark command as processing
                self.db.update_command_status(command_id, 'PROCESSING')
                
                try:
                    if command_type == 'CLOSE_POSITION':
                        # Close the specified position
                        trade = self.db.get_trade_by_id(trade_id)
                        if trade:
                            success = self.close_calendar_position(trade, "Manual close via web interface")
                            if success:
                                self.db.update_command_status(command_id, 'COMPLETED', f'Position {trade_id} closed successfully')
                                self.logger.info(f"[OK] Web command completed: Closed position {trade_id}")
                            else:
                                self.db.update_command_status(command_id, 'FAILED', f'Failed to close position {trade_id}')
                                self.logger.warning(f"[WARNING] Web command failed: Could not close position {trade_id}")
                        else:
                            self.db.update_command_status(command_id, 'FAILED', f'Trade {trade_id} not found')
                            self.logger.warning(f"[WARNING] Web command failed: Trade {trade_id} not found")
                    
                    elif command_type == 'STOP_MANAGING':
                        # Stop system management of position
                        trade = self.db.get_trade_by_id(trade_id)
                        if trade:
                            trade.status = "MANUAL_CONTROL"
                            self.db.save_trade(trade)
                            # Cancel any GTC orders
                            if trade.profit_target_order_id > 0 and trade.profit_target_status == "PLACED":
                                self.client.cancelOrder(trade.profit_target_order_id)
                                trade.profit_target_status = "CANCELLED"
                                self.db.save_trade(trade)
                            self.db.update_command_status(command_id, 'COMPLETED', f'Position {trade_id} switched to manual control')
                            self.logger.info(f"[OK] Web command completed: Position {trade_id} now under manual control")
                        else:
                            self.db.update_command_status(command_id, 'FAILED', f'Trade {trade_id} not found')
                            self.logger.warning(f"[WARNING] Web command failed: Trade {trade_id} not found")
                    
                    elif command_type == 'RUN_RECONCILIATION':
                        # Manually trigger position reconciliation
                        self.logger.info("[MANUAL] Running manual position reconciliation via web interface")
                        self.daily_position_reconciliation()
                        self.db.update_command_status(command_id, 'COMPLETED', 'Manual reconciliation completed')
                        self.logger.info(f"[OK] Web command completed: Manual reconciliation finished")
                    
                    elif command_type == 'PLACE_MISSING_GTC':
                        # Place missing GTC orders for active trades
                        self.logger.info("[MANUAL] Placing missing GTC orders via web interface")
                        result = self.place_missing_gtc_orders()
                        if result['success']:
                            self.db.update_command_status(command_id, 'COMPLETED', result['message'])
                            self.logger.info(f"[OK] Web command completed: {result['message']}")
                        else:
                            self.db.update_command_status(command_id, 'FAILED', result['message'])
                            self.logger.warning(f"[WARNING] Web command failed: {result['message']}")
                    
                    else:
                        self.db.update_command_status(command_id, 'FAILED', f'Unknown command type: {command_type}')
                        self.logger.warning(f"[WARNING] Unknown web command type: {command_type}")
                
                except Exception as cmd_error:
                    self.db.update_command_status(command_id, 'FAILED', f'Command execution error: {str(cmd_error)}')
                    self.logger.error(f"[ERROR] Web command execution failed: {cmd_error}")
            
            # Clean up old commands (older than 7 days)
            if len(commands) > 0:  # Only clean up if we processed commands
                self.db.cleanup_old_commands()
                
        except Exception as e:
            self.logger.error(f"Error processing web commands: {e}")
    
    def get_position_value(self, trade: CalendarSpread) -> Optional[float]:
        """Get current market value of the calendar spread position"""
        try:
            # Create the 4 option contracts
            short_put = self.create_spxw_contract(trade.short_expiry, trade.put_strike, "P")
            short_call = self.create_spxw_contract(trade.short_expiry, trade.call_strike, "C")
            long_put = self.create_spxw_contract(trade.long_expiry, trade.put_strike, "P")
            long_call = self.create_spxw_contract(trade.long_expiry, trade.call_strike, "C")
            
            contracts = [short_put, short_call, long_put, long_call]
            market_data = {}
            
            # Request market data for all contracts
            for i, contract in enumerate(contracts):
                req_id = self.get_next_req_id()
                self.client.reqMktData(req_id, contract, "106", False, False, [])  # Include Greeks
                market_data[i] = req_id
                time.sleep(0.1)
            
            # Wait for market data
            time.sleep(2)
            
            # Calculate current spread value (what we'd get if we closed now)
            spread_close_value = 0.0
            all_data_received = True
            
            for i, req_id in market_data.items():
                if (req_id in self.wrapper.market_data and 
                    'bid' in self.wrapper.market_data[req_id] and 
                    'ask' in self.wrapper.market_data[req_id]):
                    
                    bid = self.wrapper.market_data[req_id]['bid']
                    ask = self.wrapper.market_data[req_id]['ask']
                    mid = (bid + ask) / 2
                    
                    # Double Calendar spread closing value:
                    # We opened by BUYING the spread (paid debit)
                    # To close, we SELL the spread (receive credit)
                    if i < 2:  # Short legs (we sold these originally, now buy back)
                        spread_close_value -= mid  # We pay to buy back shorts
                    else:  # Long legs (we bought these originally, now sell)
                        spread_close_value += mid  # We receive for selling longs
                    
                    self.client.cancelMktData(req_id)
                else:
                    all_data_received = False
                    break
            
            if not all_data_received:
                self.logger.warning(f"Could not get complete market data for {trade.trade_id}")
                return None
            
            # Round to nearest nickel (0.05) like SPX options
            spread_close_value = round(spread_close_value * 20) / 20
            
            self.logger.info(f"Position value for {trade.trade_id}: Close value ${spread_close_value:.2f}")
            return spread_close_value
            
        except Exception as e:
            self.logger.error(f"Error getting position value for {trade.trade_id}: {e}")
            return None
    
    def place_profit_target_order(self, trade: CalendarSpread) -> bool:
        """Place a GTC profit target order for the calendar spread"""
        try:
            if not self.is_connected:
                self.logger.error("Cannot place profit target order - not connected to IBKR")
                return False
            
            # Calculate profit target price (50% of entry credit)
            raw_profit_target = trade.entry_credit + (trade.entry_credit * self.config.profit_target_pct)
            
            # Round DOWN to $0.10 increments for SPX options (IBKR requirement)
            profit_target_price = (int(raw_profit_target * 10) / 10.0)
            
            
            # For debit spreads, we need to sell at the profit target price
            # Since we paid entry_credit to get in, we want to receive profit_target_price to get out
            
            self.logger.info(f"Placing GTC profit target order for {trade.trade_id}")
            self.logger.info(f"Entry credit: ${trade.entry_credit:.2f}, Raw target: ${raw_profit_target:.2f}, Rounded target: ${profit_target_price:.1f}")
            
            # Create the same combo contract as we used for entry
            short_put = self.create_spxw_contract(trade.short_expiry, trade.put_strike, "PUT")
            short_call = self.create_spxw_contract(trade.short_expiry, trade.call_strike, "CALL")
            long_put = self.create_spxw_contract(trade.long_expiry, trade.long_put_strike, "PUT")
            long_call = self.create_spxw_contract(trade.long_expiry, trade.long_call_strike, "CALL")
            
            contracts = [short_put, short_call, long_put, long_call]
            
            # Clear any existing contract details
            self.wrapper.contract_details = {}
            
            # Ensure we're still connected before requesting contract details
            if not self.is_connected or not self.client.isConnected():
                self.logger.error("Lost connection to IBKR during GTC order setup")
                return False

            # Get contract details for all legs with unique request IDs
            detail_req_ids = []
            for i, contract in enumerate(contracts):
                # Double-check connection before each request
                if not self.client.isConnected():
                    self.logger.error(f"Connection lost before contract detail request {i}")
                    return False
                    
                req_id = 8000 + i  # Use different range from other requests
                detail_req_ids.append(req_id)
                self.client.reqContractDetails(req_id, contract)
                self.logger.info(f"Requesting contract details for leg {i}: {contract.symbol} {contract.strike}{contract.right} {contract.lastTradeDateOrContractMonth}")
                time.sleep(0.5)  # Longer delay between requests for stability
            
            # Wait for contract details with timeout
            max_wait_time = 15  # Increased timeout for better reliability
            start_time = time.time()
            
            while len(self.wrapper.contract_details) < len(contracts) and (time.time() - start_time) < max_wait_time:
                time.sleep(0.5)
            
            # If still missing contract details, try requesting them again
            if len(self.wrapper.contract_details) < len(contracts):
                self.logger.warning(f"Only got {len(self.wrapper.contract_details)}/{len(contracts)} contract details on first attempt, retrying...")
                
                # Check connection before retry
                if not self.client.isConnected():
                    self.logger.error("Connection lost during contract detail resolution - cannot retry")
                    return False
                
                time.sleep(2)  # Brief pause
                
                # Clear and retry
                self.wrapper.contract_details = {}
                for i, contract in enumerate(contracts):
                    if not self.client.isConnected():
                        self.logger.error(f"Connection lost during retry at contract {i}")
                        return False
                        
                    req_id = 8100 + i  # Use different range for retry
                    self.client.reqContractDetails(req_id, contract)
                    time.sleep(0.5)  # Longer delay for stability
                
                # Wait again
                start_time = time.time()
                while len(self.wrapper.contract_details) < len(contracts) and (time.time() - start_time) < 10:
                    time.sleep(0.5)
            
            # Check if we got all contract details
            if len(self.wrapper.contract_details) < len(contracts):
                self.logger.error(f"Only received {len(self.wrapper.contract_details)}/{len(contracts)} contract details for profit target")
                return False
            
            # Update contracts with resolved details
            for i, req_id in enumerate(detail_req_ids):
                if req_id in self.wrapper.contract_details and self.wrapper.contract_details[req_id]:
                    # contract_details[req_id] is a list, get the first (and usually only) item
                    contract_detail = self.wrapper.contract_details[req_id][0]
                    contracts[i] = contract_detail.contract
                    self.logger.info(f"Profit target contract {i} resolved: conId={contracts[i].conId}")
                else:
                    self.logger.error(f"Failed to resolve profit target contract {i}")
                    return False
            
            # Verify we have all contract IDs
            if not all(hasattr(contract, 'conId') and contract.conId > 0 for contract in contracts):
                self.logger.error("Some profit target contracts still have invalid conId after resolution")
                return False
            
            # Keep the original 50% profit target now that we have the correct order logic
            
            # Create the combo contract for closing (BUY to close)
            combo = self.create_closing_combo_contract(contracts)
            
            # Get next order ID
            if self.wrapper.next_order_id is None:
                self.client.reqIds(-1)
                time.sleep(1)
            
            profit_target_order_id = self.wrapper.next_order_id
            self.wrapper.next_order_id += 1
            
            # Create GTC limit order using IBKR's counterintuitive combo logic
            # For debit spreads: we BUY to close with NEGATIVE price (matches manual close logic)
            profit_order = Order()
            profit_order.action = "BUY"  # BUY to close (IBKR's counterintuitive logic)
            profit_order.orderType = "LMT"
            profit_order.lmtPrice = -profit_target_price  # NEGATIVE price for debit spread closing
            profit_order.totalQuantity = self.config.position_size
            profit_order.tif = "GTC"  # Good Till Cancelled
            profit_order.transmit = True
            profit_order.eTradeOnly = False
            profit_order.firmQuoteOnly = False
            profit_order.outsideRth = True
            profit_order.parentPermId = 0  # Ensure no parent order constraints
            
            self.logger.info(f"Placing GTC order: ID={profit_target_order_id}, BUY at ${-profit_target_price:.2f} (negative), Qty={self.config.position_size}")
            self.logger.info(f"GTC Logic: Bought spread for ${trade.entry_credit:.2f}, closing via BUY at negative ${profit_target_price:.2f} (IBKR's counterintuitive logic)")
            
            # Place the order
            self.client.placeOrder(profit_target_order_id, combo, profit_order)
            
            # Wait briefly for order acknowledgment
            time.sleep(3)
            
            # Check if order was accepted
            order_placed_successfully = False
            if profit_target_order_id in self.wrapper.orders:
                order_status = self.wrapper.orders[profit_target_order_id].get('status', 'Unknown')
                self.logger.info(f"Profit target order status: {order_status}")
                
                if order_status in ['Submitted', 'PreSubmitted']:
                    order_placed_successfully = True
                elif order_status in ['Cancelled', 'Rejected']:
                    self.logger.error(f"Profit target order {order_status.lower()}: {profit_target_order_id}")
                    return False
                else:
                    # Give it a bit more time for slow responses
                    time.sleep(2)
                    if profit_target_order_id in self.wrapper.orders:
                        updated_status = self.wrapper.orders[profit_target_order_id].get('status', 'Unknown')
                        if updated_status in ['Submitted', 'PreSubmitted']:
                            order_placed_successfully = True
                        else:
                            self.logger.error(f"Profit target order final status: {updated_status}")
            else:
                self.logger.warning("No immediate order status received for profit target order")
                # Give it more time and check again
                time.sleep(3)
                if profit_target_order_id in self.wrapper.orders:
                    order_status = self.wrapper.orders[profit_target_order_id].get('status', 'Unknown')
                    if order_status in ['Submitted', 'PreSubmitted']:
                        order_placed_successfully = True
                    else:
                        self.logger.error(f"Delayed profit target order status: {order_status}")
                else:
                    self.logger.error("No order status received after extended wait - GTC order likely rejected")
                    self.logger.error(f"Available order IDs in wrapper: {list(self.wrapper.orders.keys())}")
                    # Check for any error messages in wrapper
                    if hasattr(self.wrapper, 'connection_errors') and self.wrapper.connection_errors:
                        recent_errors = [err for err in self.wrapper.connection_errors if time.time() - err['timestamp'] < 30]
                        if recent_errors:
                            self.logger.error(f"Recent IBKR errors: {recent_errors}")
            
            if order_placed_successfully:
                # Update trade record
                trade.profit_target_order_id = profit_target_order_id
                trade.profit_target_price = profit_target_price
                trade.profit_target_status = "PLACED"
                
                # Save to database
                self.db.save_trade(trade)
                
                self.logger.info(f"[ACTIVE] GTC Profit target order ACTIVE: Order {profit_target_order_id} at ${profit_target_price:.2f}")
                
                # Send notification
                self.notifications.send_sms(
                    f"SPX Calendar: GTC profit target order ACTIVE for {trade.trade_id}. "
                    f"Target: ${profit_target_price:.2f} (Order {profit_target_order_id})"
                )
                
                return True
            else:
                self.logger.error("Failed to confirm profit target order placement")
                return False
            
        except Exception as e:
            self.logger.error(f"Error placing profit target order: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    def get_current_combo_price(self, contracts) -> float:
        """Get current market price for a combo contract"""
        try:
            # Create combo contract for pricing
            combo = self.create_closing_combo_contract(contracts)
            
            # Request market data for the combo
            req_id = 9000  # Use high ID to avoid conflicts
            self.wrapper.market_data[req_id] = {}
            
            # Request market data
            self.client.reqMktData(req_id, combo, "", False, False, [])
            
            # Wait for market data
            start_time = time.time()
            timeout = 5  # 5 second timeout
            
            while (time.time() - start_time) < timeout:
                if req_id in self.wrapper.market_data:
                    data = self.wrapper.market_data[req_id]
                    # Look for bid/ask or last price
                    if 'bid' in data and 'ask' in data and data['bid'] > 0 and data['ask'] > 0:
                        mid_price = (data['bid'] + data['ask']) / 2
                        self.logger.info(f"Current combo price: Bid=${data['bid']:.2f}, Ask=${data['ask']:.2f}, Mid=${mid_price:.2f}")
                        # Cancel market data subscription
                        self.client.cancelMktData(req_id)
                        return mid_price
                    elif 'last' in data and data['last'] > 0:
                        self.logger.info(f"Current combo price (last): ${data['last']:.2f}")
                        # Cancel market data subscription
                        self.client.cancelMktData(req_id)
                        return data['last']
                
                time.sleep(0.2)
            
            # Cancel market data subscription if we got here
            self.client.cancelMktData(req_id)
            self.logger.warning("Could not get current combo price within timeout")
            return 0.0
            
        except Exception as e:
            self.logger.error(f"Error getting current combo price: {e}")
            return 0.0

    def place_missing_gtc_orders(self) -> dict:
        """Place GTC orders for active trades that are missing them - fallback function"""
        try:
            if not self.is_connected:
                return {"success": False, "message": "Not connected to IBKR"}
            
            active_trades = self.db.get_active_trades()
            if not active_trades:
                return {"success": True, "message": "No active trades found"}
            
            # Find trades missing GTC orders
            missing_gtc = []
            for trade in active_trades:
                if trade.status == "ACTIVE" and (trade.profit_target_order_id == 0 or trade.profit_target_status == "NONE"):
                    missing_gtc.append(trade)
            
            if not missing_gtc:
                return {"success": True, "message": f"All {len(active_trades)} active trades already have GTC orders"}
            
            # Attempt to place missing GTC orders
            success_count = 0
            failed_trades = []
            
            for trade in missing_gtc:
                self.logger.info(f"Attempting to place missing GTC order for {trade.trade_id}")
                success = self.place_profit_target_order(trade)
                if success:
                    success_count += 1
                    self.logger.info(f"Successfully placed GTC order for {trade.trade_id}")
                else:
                    failed_trades.append(trade.trade_id)
                    self.logger.error(f"Failed to place GTC order for {trade.trade_id}")
                
                # Small delay between orders
                time.sleep(1)
            
            # Prepare result message
            total_missing = len(missing_gtc)
            if success_count == total_missing:
                message = f"Successfully placed {success_count} missing GTC orders"
                self.logger.info(f"GTC Repair Complete: {success_count}/{total_missing} orders placed")
            elif success_count > 0:
                message = f"Placed {success_count}/{total_missing} GTC orders. Failed: {', '.join(failed_trades)}"
                self.logger.warning(f"GTC Repair Partial: {success_count}/{total_missing} orders placed")
            else:
                message = f"Failed to place any GTC orders for {total_missing} trades"
                self.logger.error(f"GTC Repair Failed: 0/{total_missing} orders placed")
            
            return {
                "success": success_count > 0,
                "message": message,
                "placed": success_count,
                "total_missing": total_missing,
                "failed_trades": failed_trades
            }
            
        except Exception as e:
            error_msg = f"Error in place_missing_gtc_orders: {e}"
            self.logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def check_profit_target_fill(self, order_id: int, fill_price: float):
        """Check if a filled order is a profit target order and handle it"""
        try:
            # Find the trade with this profit target order ID
            active_trades = self.db.get_active_trades()
            for trade in active_trades:
                if trade.profit_target_order_id == order_id and trade.profit_target_status == "PLACED":
                    self.logger.info(f"🎯 Profit target hit for {trade.trade_id}! Order {order_id} filled at ${abs(fill_price):.2f}")
                    
                    # Update trade status
                    trade.status = "CLOSED"
                    trade.exit_reason = "Profit target reached (GTC order)"
                    trade.exit_credit = abs(fill_price)  # Convert negative price to positive
                    trade.profit_target_status = "FILLED"
                    
                    # Calculate P&L
                    trade.realized_pnl = trade.exit_credit - trade.entry_credit
                    
                    # Set exit date/time
                    current_time = self.get_local_time()
                    trade.exit_date = current_time.strftime('%Y-%m-%d')
                    trade.exit_time = current_time.strftime('%H:%M:%S')
                    trade.exit_spx_price = self.current_spx_price if hasattr(self, 'current_spx_price') else 0.0
                    
                    # Save to database
                    self.db.save_trade(trade)
                    
                    # Log the successful exit
                    self.db.log_daily_action(
                        'PROFIT_TARGET_HIT',
                        f'Position {trade.trade_id} closed via profit target. Entry: ${trade.entry_credit:.2f}, Exit: ${trade.exit_credit:.2f}, P&L: ${trade.realized_pnl:.2f}',
                        True
                    )
                    
                    # Send notification
                    self.notifications.send_sms(
                        f"SPX Calendar: 🎯 Profit target hit! {trade.trade_id} closed at ${trade.exit_credit:.2f}. "
                        f"P&L: ${trade.realized_pnl:.2f} ({(trade.realized_pnl/trade.entry_credit)*100:.1f}%)"
                    )
                    
                    self.logger.info(f"✅ Position {trade.trade_id} closed successfully via profit target")
                    return True
                    
        except Exception as e:
            self.logger.error(f"Error handling profit target fill: {e}")
        return False

    def check_gtc_order_status(self):
        """Debug method to check status of all active GTC profit target orders"""
        try:
            active_trades = self.db.get_active_trades()
            gtc_orders = [trade for trade in active_trades if trade.profit_target_order_id > 0 and trade.profit_target_status == "PLACED"]
            
            if not gtc_orders:
                self.logger.info("No active GTC profit target orders found")
                return
            
            self.logger.info(f"Checking {len(gtc_orders)} active GTC profit target orders:")
            
            for trade in gtc_orders:
                order_id = trade.profit_target_order_id
                
                # Check if we have status info for this order
                if order_id in self.wrapper.orders:
                    order_info = self.wrapper.orders[order_id]
                    status = order_info.get('status', 'Unknown')
                    filled = order_info.get('filled', 0)
                    remaining = order_info.get('remaining', 0)
                    
                    self.logger.info(f"  Order {order_id} ({trade.trade_id}): {status}, Filled: {filled}, Remaining: {remaining}")
                    
                    # If order was cancelled or rejected, update database
                    if status in ['Cancelled', 'Rejected', 'ApiCancelled']:
                        self.logger.warning(f"  ⚠️ GTC order {order_id} is {status} - updating database")
                        trade.profit_target_status = status.upper()
                        self.db.save_trade(trade)
                        
                        # Send alert about cancelled GTC order
                        self.notifications.send_sms(
                            f"ALERT: GTC profit target for {trade.trade_id} was {status.lower()}. "
                            f"Manual monitoring required."
                        )
                else:
                    self.logger.warning(f"  ⚠️ No status info for GTC order {order_id} ({trade.trade_id})")
                    
        except Exception as e:
            self.logger.error(f"Error checking GTC order status: {e}")

    def request_open_orders(self):
        """Request all open orders from IBKR to refresh our order status"""
        try:
            if self.is_connected:
                self.logger.info("Requesting all open orders from IBKR...")
                self.client.reqAllOpenOrders()
                time.sleep(2)  # Give time for response
                self.logger.info(f"Current order count in memory: {len(self.wrapper.orders)}")
            else:
                self.logger.warning("Cannot request open orders - not connected to IBKR")
        except Exception as e:
            self.logger.error(f"Error requesting open orders: {e}")

    def close_calendar_position(self, trade: CalendarSpread, reason: str):
        """Close a calendar spread position"""
        try:
            self.logger.info(f"Starting close process for {trade.trade_id}")
            
            # Cancel any outstanding profit target order first
            if trade.profit_target_order_id > 0 and trade.profit_target_status == "PLACED":
                self.logger.info(f"Cancelling profit target order {trade.profit_target_order_id}")
                self.client.cancelOrder(trade.profit_target_order_id)
                trade.profit_target_status = "CANCELLED"
                self.db.save_trade(trade)
            
            # Create the 4 option contracts
            short_put = self.create_spxw_contract(trade.short_expiry, trade.put_strike, "P")
            short_call = self.create_spxw_contract(trade.short_expiry, trade.call_strike, "C")
            
            # Use actual long strikes (may differ from short strikes for adjusted trades)
            long_put_strike = trade.long_put_strike if trade.long_put_strike != 0.0 else trade.put_strike
            long_call_strike = trade.long_call_strike if trade.long_call_strike != 0.0 else trade.call_strike
            
            self.logger.info(f"Contract strikes - Short: {trade.put_strike}P/{trade.call_strike}C, Long: {long_put_strike}P/{long_call_strike}C")
            
            long_put = self.create_spxw_contract(trade.long_expiry, long_put_strike, "P")
            long_call = self.create_spxw_contract(trade.long_expiry, long_call_strike, "C")
            
            contracts = [short_put, short_call, long_put, long_call]
            
            # Get current market value for closing price and capture exit Greeks
            self.logger.info(f"Getting position value for closing {trade.trade_id}...")
            current_value, market_data_dict = self.get_position_value_with_greeks(trade)
            self.logger.info(f"Position value result: {current_value}")
            
            if current_value is None:
                self.logger.error(f"Could not get market value to close {trade.trade_id}")
                # Write to debug file as well
                with open('web_debug.log', 'a') as f:
                    f.write(f"  ERROR: Could not get market value for {trade.trade_id}\n")
                return False
            
            # Capture exit Greeks and IV
            self.logger.info("Capturing exit Greeks and IV...")
            self._capture_exit_greeks(trade, market_data_dict)
            
            # Cancel market data requests after capturing Greeks
            for req_id in market_data_dict.values():
                self.client.cancelMktData(req_id)
            
            # Get contract details for all 4 legs to populate conId
            self.logger.info("Requesting contract details for closing order...")
            for i, contract in enumerate(contracts):
                req_id = self.req_id_counter
                self.req_id_counter += 1
                
                self.wrapper.contract_details_received.clear()
                self.client.reqContractDetails(req_id, contract)
                
                # Wait for contract details (increased timeout)
                self.logger.info(f"Waiting for contract details for contract {i} (req_id {req_id})")
                if self.wrapper.contract_details_received.wait(30):  # 30 second timeout
                    if req_id in self.wrapper.contract_details:
                        contract_detail = self.wrapper.contract_details[req_id][0]
                        contracts[i].conId = contract_detail.contract.conId
                        self.logger.info(f"Contract {i} conId: {contracts[i].conId}")
                        
                        # Write to debug file as well
                        with open('web_debug.log', 'a') as f:
                            f.write(f"  Contract {i} resolved: conId={contracts[i].conId}\n")
                    else:
                        self.logger.error(f"No contract details received for contract {i} (req_id {req_id})")
                        with open('web_debug.log', 'a') as f:
                            f.write(f"  ERROR: No contract details for contract {i}\n")
                        return False
                else:
                    self.logger.error(f"Timeout getting contract details for contract {i} (req_id {req_id})")
                    with open('web_debug.log', 'a') as f:
                        f.write(f"  ERROR: Timeout getting contract details for contract {i}\n")
                    return False
            
            # Get fresh order ID using database tracking (more reliable than IBKR reqIds)
            self.logger.info("Getting fresh order ID using database tracking...")
            
            # Order IDs will be generated fresh for each attempt inside the loop
            
            # For closing, we reverse the original trade
            # Original was BUY the spread, so closing is SELL the spread
            # Use limit order with price improvement logic (same as entry but reversed)
            
            # Get current spread mid-price
            current_value, _ = self.get_position_value_with_greeks(trade)
            if current_value is None:
                self.logger.error("Could not get current spread value for closing price")
                return False
            
            spread_mid = current_value
            self.logger.info(f"Current spread mid price for closing: ${spread_mid:.2f}")
            
            # Create combo contract for closing
            combo_contract = self.create_closing_combo_contract(contracts)
            
            # Debug: Log combo contract details
            self.logger.info(f"Combo contract: {len(combo_contract.comboLegs)} legs")
            for i, leg in enumerate(combo_contract.comboLegs):
                self.logger.info(f"  Leg {i}: conId={leg.conId}, action={leg.action}, ratio={leg.ratio}")
                with open('web_debug.log', 'a') as f:
                    f.write(f"  Leg {i}: conId={leg.conId}, action={leg.action}\n")
            
            # Attempt to fill the closing order with price improvement logic
            # Since we're BUYING the spread to close, we go DOWN in price if not filled
            for attempt in range(self.config.max_price_attempts):
                self.logger.info(f"Closing attempt {attempt + 1} with price ${-spread_mid:.2f} (negative for debit spread closing)")
                
                # Create limit order
                close_order = Order()
                close_order.action = "BUY"  # BUY the spread to close (IBKR's counterintuitive logic)
                close_order.orderType = "LMT"
                close_order.lmtPrice = -spread_mid  # Negative price for debit spread closing
                close_order.totalQuantity = self.config.position_size
                close_order.transmit = True
                
                # Clear any potentially problematic attributes (same as entry orders)
                close_order.eTradeOnly = False
                close_order.firmQuoteOnly = False
                close_order.outsideRth = True  # Allow closing outside RTH
                
                # Use IBKR's next_order_id and increment it properly
                if self.wrapper.next_order_id is None:
                    self.logger.error("No valid order ID from IBKR")
                    return False

                order_id = self.wrapper.next_order_id
                self.wrapper.next_order_id += 1  # Increment for next use

                self.logger.info(f"Using IBKR order ID {order_id}")
                with open('web_debug.log', 'a') as f:
                    f.write(f"  Using IBKR order ID {order_id}\n")
                
                # Store the order ID we're waiting for to avoid confusion with other orders
                waiting_for_order_id = order_id
                
                # Place the closing order
                self.logger.info(f"About to place closing order {order_id} with IBKR...")
                try:
                    self.client.placeOrder(order_id, combo_contract, close_order)
                    self.logger.info(f"Closing order placed (attempt {attempt + 1}): ID {order_id}, Price ${spread_mid:.2f}")
                    
                    with open('web_debug.log', 'a') as f:
                        f.write(f"  Closing attempt {attempt + 1}: Order {order_id} at ${-spread_mid:.2f} (negative for debit spread)\n")
                        f.write(f"  Order placed successfully with IBKR\n")
                        
                except Exception as e:
                    self.logger.error(f"Failed to place closing order {order_id}: {e}")
                    with open('web_debug.log', 'a') as f:
                        f.write(f"  ERROR placing order {order_id}: {e}\n")
                    continue  # Try next attempt
                
                # Wait for fill and check for immediate errors
                fill_timeout = self.config.fill_wait_time
                start_time = time.time()
                
                # Give IBKR a moment to respond with any immediate errors
                time.sleep(2)
                
                # Check if there were any connection errors
                if self.wrapper.connection_errors:
                    latest_error = self.wrapper.connection_errors[-1]
                    self.logger.error(f"IBKR connection error after placing order: {latest_error}")
                    with open('web_debug.log', 'a') as f:
                        f.write(f"  IBKR ERROR: {latest_error}\n")
                
                while time.time() - start_time < fill_timeout:
                    if waiting_for_order_id in self.wrapper.orders:
                        order_status = self.wrapper.orders[waiting_for_order_id]['status']
                        
                        if order_status == 'Filled':
                            # Position closed successfully
                            exit_price = self.wrapper.orders[waiting_for_order_id]['avg_fill_price']
                            # For debit spreads: P&L = exit_credit - entry_debit
                            # exit_price is negative from IBKR, so we convert to positive credit
                            exit_credit = abs(exit_price)  # Convert negative to positive
                            pnl = exit_credit - trade.entry_credit  # exit_credit - entry_debit
                            
                            # Update trade record
                            trade.status = "CLOSED"
                            trade.exit_reason = reason
                            trade.exit_date = self.get_local_time().strftime('%Y-%m-%d')
                            trade.exit_time = self.get_local_time().strftime('%H:%M:%S')
                            trade.exit_credit = exit_credit  # Store the calculated positive credit
                            trade.realized_pnl = pnl
                            
                            self.db.save_trade(trade)
                            
                            self.logger.info(f"Position closed: {trade.trade_id}, P&L: ${pnl:.2f}")
                            self.notifications.notify_position_closed(trade, pnl)
                            
                            with open('web_debug.log', 'a') as f:
                                f.write(f"  SUCCESS: Order {waiting_for_order_id} filled at ${exit_price:.2f}, P&L: ${pnl:.2f}\n")
                            return True
                        elif order_status in ['Cancelled', 'ApiCancelled']:
                            self.logger.warning(f"Closing order {waiting_for_order_id} was cancelled")
                            break  # Try next attempt
                    
                    time.sleep(1)
                
                # Order didn't fill, cancel it and try with worse price
                self.client.cancelOrder(waiting_for_order_id)
                self.logger.info(f"Closing order {waiting_for_order_id} not filled, cancelled")
                
                # For closing (BUYING), go DOWN in price for next attempt
                # Since we use negative prices, we subtract from the negative value (making it more negative)
                spread_mid -= self.config.price_increment  # Decrease price by $0.05
                
                time.sleep(2)  # Brief pause between attempts
            
            # All attempts failed
            self.logger.warning(f"⚠️ All closing attempts failed for {trade.trade_id}")
            with open('web_debug.log', 'a') as f:
                f.write(f"  ERROR: All {self.config.max_price_attempts} closing attempts failed\n")
            return False
            
        except Exception as e:
            self.logger.error(f"Error closing position {trade.trade_id}: {e}")
            return False
    
    def check_ghost_strikes(self, put_strike: float, call_strike: float) -> Tuple[bool, Optional[str]]:
        """
        Check for ghost strikes against last week's active trade
        Returns: (has_conflict, conflict_description)
        """
        try:
            # Get last week's active trade (21/28 day structure means only last week can conflict)
            current_date = self.get_local_time().date()
            last_week = current_date - timedelta(days=7)
            
            # Get active trades from last week (within 6-8 days ago to account for weekends)
            active_trades = self.db.get_active_trades()
            last_week_trade = None
            
            for trade in active_trades:
                trade_date = datetime.strptime(trade.entry_date, '%Y-%m-%d').date()
                days_ago = (current_date - trade_date).days
                if 6 <= days_ago <= 8:  # Last week's trade
                    last_week_trade = trade
                    break
            
            if not last_week_trade:
                return False, None
            
            # Check if today's short strikes match last week's long strikes
            conflicts = []
            
            # Check put conflict
            last_week_long_put = last_week_trade.long_put_strike if last_week_trade.long_put_strike != 0.0 else last_week_trade.put_strike
            if put_strike == last_week_long_put:
                conflicts.append(f"Put {put_strike}")
            
            # Check call conflict
            last_week_long_call = last_week_trade.long_call_strike if last_week_trade.long_call_strike != 0.0 else last_week_trade.call_strike
            if call_strike == last_week_long_call:
                conflicts.append(f"Call {call_strike}")
            
            if conflicts:
                conflict_desc = f"Ghost strike conflict with {last_week_trade.trade_id}: {', '.join(conflicts)}"
                return True, conflict_desc
            
            return False, None
            
        except Exception as e:
            self.logger.error(f"Error checking ghost strikes: {e}")
            return False, None
    
    def adjust_ghost_strike(self, strike: float, option_type: str, expiry: str) -> float:
        """
        Adjust a conflicting strike by moving one strike up or down based on delta
        Returns the adjusted strike that's closest to target delta
        """
        try:
            target_delta = self.config.target_delta
            
            # Get current option chain for the expiry
            option_chain = self.get_option_chain_for_expiry(expiry)
            if not option_chain:
                self.logger.error(f"No option chain available for {expiry}")
                return strike
            
            # Filter for the correct option type
            options = [opt for opt in option_chain if opt['right'] == option_type]
            if not options:
                return strike
            
            # Sort by strike
            options.sort(key=lambda x: x['strike'])
            
            # Find current strike index
            current_index = None
            for i, opt in enumerate(options):
                if opt['strike'] == strike:
                    current_index = i
                    break
            
            if current_index is None:
                self.logger.error(f"Current strike {strike} not found in option chain")
                return strike
            
            # Check strikes one up and one down
            candidates = []
            
            # One strike up
            if current_index + 1 < len(options):
                up_option = options[current_index + 1]
                up_delta = abs(up_option.get('delta', 0))
                up_diff = abs(up_delta - target_delta)
                candidates.append((up_option['strike'], up_diff, up_delta))
            
            # One strike down
            if current_index - 1 >= 0:
                down_option = options[current_index - 1]
                down_delta = abs(down_option.get('delta', 0))
                down_diff = abs(down_delta - target_delta)
                candidates.append((down_option['strike'], down_diff, down_delta))
            
            if not candidates:
                self.logger.warning(f"No alternative strikes available for {strike}")
                return strike
            
            # Choose the strike with delta closest to target
            best_strike, best_diff, best_delta = min(candidates, key=lambda x: x[1])
            
            self.logger.info(f"Adjusted {option_type} strike from {strike} to {best_strike} (delta: {best_delta:.3f})")
            return best_strike
            
        except Exception as e:
            self.logger.error(f"Error adjusting ghost strike: {e}")
            return strike
    
    def create_closing_combo_contract(self, contracts: List[Contract]) -> Contract:
        """Create combo contract for closing the position (reverse of opening)"""
        combo = Contract()
        combo.symbol = "SPX"
        combo.secType = "BAG"
        combo.exchange = "SMART"
        combo.currency = "USD"
        
        # Define the legs (reverse of opening)
        from ibapi.contract import ComboLeg
        combo.comboLegs = []
        
        # contracts = [short_put, short_call, long_put, long_call]
        
        # Buy back short put - contracts[0]
        leg1 = ComboLeg()
        leg1.conId = contracts[0].conId if contracts[0].conId else 0
        leg1.ratio = 1
        leg1.action = "BUY"  # Buy back what we sold
        leg1.exchange = "SMART"
        combo.comboLegs.append(leg1)
        
        # Buy back short call - contracts[1]
        leg2 = ComboLeg()
        leg2.conId = contracts[1].conId if contracts[1].conId else 0
        leg2.ratio = 1
        leg2.action = "BUY"  # Buy back what we sold
        leg2.exchange = "SMART"
        combo.comboLegs.append(leg2)
        
        # Sell long put - contracts[2]
        leg3 = ComboLeg()
        leg3.conId = contracts[2].conId if contracts[2].conId else 0
        leg3.ratio = 1
        leg3.action = "SELL"  # Sell what we bought
        leg3.exchange = "SMART"
        combo.comboLegs.append(leg3)
        
        # Sell long call - contracts[3]
        leg4 = ComboLeg()
        leg4.conId = contracts[3].conId if contracts[3].conId else 0
        leg4.ratio = 1
        leg4.action = "SELL"  # Sell what we bought
        leg4.exchange = "SMART"
        combo.comboLegs.append(leg4)
        
        return combo

# ===============================================
# MANUAL OVERRIDE INTERFACE
# ===============================================

class ManualOverride:
    """Manual override interface for impatient exits"""
    
    def __init__(self, trader: SPXCalendarTrader):
        self.trader = trader
    
    def list_active_positions(self):
        """List all active positions"""
        active_trades = self.trader.db.get_active_trades()
        if not active_trades:
            print("No active positions")
            return
        
        # Separate active from manual control
        auto_trades = [t for t in active_trades if t.status == "ACTIVE"]
        manual_trades = [t for t in active_trades if t.status == "MANUAL_CONTROL"]
        
        if auto_trades:
            print("\n=== ACTIVE SPX CALENDAR POSITIONS (AUTO-MANAGED) ===")
            self._display_trades(auto_trades, 0)
        
        if manual_trades:
            print("\n=== MANUAL CONTROL POSITIONS (YOU MANAGE) ===")
            self._display_trades(manual_trades, len(auto_trades))
        
        return active_trades
    
    def _display_trades(self, trades, start_index):
        """Display a list of trades with proper numbering"""
        for i, trade in enumerate(trades):
            # Get current value and P&L
            current_value = self.trader.get_position_value(trade) if trade.status == "ACTIVE" else trade.current_value
            # For debit spreads: P&L = current_close_value - entry_debit
            pnl = (current_value if current_value else 0) - trade.entry_credit  
            pnl_pct = (pnl / abs(trade.entry_credit) * 100) if trade.entry_credit != 0 else 0
            
            print(f"{start_index + i + 1}. ID: {trade.trade_id}")
            print(f"   Entry: {trade.entry_date} {trade.entry_time}")
            print(f"   SPX @ Entry: ${trade.spx_price:.2f}")
            print(f"   Strikes: {trade.put_strike}P / {trade.call_strike}C")
            print(f"   Expiries: {trade.short_expiry} / {trade.long_expiry}")
            print(f"   Entry Debit: ${trade.entry_credit:.2f}")
            print(f"   Profit Target: ${trade.profit_target:.2f}")
            print(f"   Current Value: ${current_value:.2f}" if current_value else "   Current Value: N/A")
            print(f"   Unrealized P&L: ${pnl:.2f} ({pnl_pct:.1f}%)")
            print(f"   Status: {trade.status}")
            
            # Calculate days to expiry
            if trade.status == "ACTIVE":
                try:
                    short_expiry_date = datetime.strptime(trade.short_expiry, '%Y%m%d').date()
                    days_to_expiry = (short_expiry_date - datetime.now().date()).days
                    print(f"   Days to Short Expiry: {days_to_expiry}")
                    
                    entry_date = datetime.strptime(trade.entry_date, '%Y-%m-%d').date()
                    days_since_entry = (datetime.now().date() - entry_date).days
                    print(f"   Days Since Entry: {days_since_entry}")
                except:
                    pass
            
            print("-" * 50)
    
    def force_close_position(self, trade_id: str, reason: str = "Manual override"):
        """Manually close a specific position"""
        try:
            # Find the trade
            active_trades = self.trader.db.get_active_trades()
            trade_to_close = None
            
            for trade in active_trades:
                if trade.trade_id == trade_id:
                    trade_to_close = trade
                    break
            
            if not trade_to_close:
                print(f"ERROR: Trade {trade_id} not found or not active")
                return False
            
            if not self.trader.is_connected:
                if not self.trader.connect_to_ibkr():
                    print("ERROR: Could not connect to IBKR")
                    return False
            
            print(f" Manually closing position {trade_id}...")
            print(f"   Reason: {reason}")
            print(f"   Current status: {trade_to_close.status}")
            
            # Close the position
            success = self.trader.close_calendar_position(trade_to_close, reason)
            
            if success:
                print(f"SUCCESS: Position {trade_id} closed successfully")
            else:
                print(f"ERROR: Failed to close position {trade_id}")
            
            return success
            
        except Exception as e:
            print(f"ERROR: Error closing position: {e}")
            return False
    
    def force_close_by_number(self, position_number: int, reason: str = "Manual override"):
        """Close position by its number in the list"""
        active_trades = self.list_active_positions()
        
        if not active_trades or position_number < 1 or position_number > len(active_trades):
            print(f"Invalid position number: {position_number}")
            return False
        
        trade = active_trades[position_number - 1]
        return self.force_close_position(trade.trade_id, reason)
    
    def take_over_position(self, position_number: int):
        """Take over a position for manual management (legging out)"""
        active_trades = self.trader.db.get_active_trades()
        
        if not active_trades or position_number < 1 or position_number > len(active_trades):
            print(f"Invalid position number: {position_number}")
            return False
        
        trade = active_trades[position_number - 1]
        
        print(f"\n⚠️ MANUAL TAKEOVER WARNING ⚠️")
        print(f"You are about to take manual control of position {trade.trade_id}")
        print(f"Entry: {trade.entry_date} {trade.entry_time}")
        print(f"Strikes: {trade.put_strike}P / {trade.call_strike}C")
        print(f"Expiries: {trade.short_expiry} / {trade.long_expiry}")
        print(f"Entry Debit: ${trade.entry_credit:.2f}")
        
        current_value = self.trader.get_position_value(trade)
        if current_value:
            pnl = current_value - trade.entry_credit
            print(f"Current Value: ${current_value:.2f}")
            print(f"Unrealized P&L: ${pnl:.2f}")
        
        print(f"\n📋 POSITION LEGS:")
        print(f"1. SHORT {trade.put_strike} PUT  {trade.short_expiry} (21-day)")
        print(f"2. SHORT {trade.call_strike} CALL {trade.short_expiry} (21-day)")
        print(f"3. LONG  {trade.put_strike} PUT  {trade.long_expiry} (28-day)")
        print(f"4. LONG  {trade.call_strike} CALL {trade.long_expiry} (28-day)")
        
        print(f"\n⚠️ This will mark the position as 'MANUAL_CONTROL' in the system.")
        print(f"⚠️ The automated system will no longer manage this position.")
        print(f"⚠️ You will be responsible for all future management.")
        print(f"⚠️ Consider the risks of legging out - you may create unhedged exposure.")
        
        confirm = input("\nType 'TAKEOVER' to confirm manual control: ").strip()
        
        if confirm != "TAKEOVER":
            print("ERROR: Manual takeover cancelled")
            return False
        
        # Update trade status to manual control
        trade.status = "MANUAL_CONTROL"
        trade.exit_reason = "Manual takeover for legging out"
        trade.exit_date = datetime.now().strftime('%Y-%m-%d')
        trade.exit_time = datetime.now().strftime('%H:%M:%S')
        
        # Save the updated trade
        self.trader.db.save_trade(trade)
        
        # Log the takeover
        self.trader.db.log_daily_action(
            "MANUAL_TAKEOVER", 
            f"Position {trade.trade_id} taken over for manual management", 
            True
        )
        
        print(f"SUCCESS: Position {trade.trade_id} is now under manual control")
        print(f" Position details saved for your records")
        print(f" Original profit target was: ${trade.profit_target:.2f}")
        
        # Send notification
        if current_value:
            pnl = current_value - trade.entry_credit
            message = (f"SPX Calendar MANUAL TAKEOVER: {trade.trade_id}. "
                      f"Strikes: {trade.put_strike}/{trade.call_strike}. "
                      f"Current P&L: ${pnl:.2f}")
        else:
            message = (f"SPX Calendar MANUAL TAKEOVER: {trade.trade_id}. "
                      f"Strikes: {trade.put_strike}/{trade.call_strike}. "
                      f"Current P&L: N/A")
        self.trader.notifications.send_sms(message)
        
        # Offer to display individual leg values
        show_legs = input("\nShow individual leg values? (y/n): ").strip().lower()
        if show_legs == 'y':
            self.show_individual_leg_values(trade)
        
        return True
    
    def show_individual_leg_values(self, trade: CalendarSpread):
        """Show current values of individual legs for manual management"""
        try:
            print(f"\n INDIVIDUAL LEG VALUES for {trade.trade_id}")
            print("=" * 60)
            
            # Create the 4 option contracts
            contracts = [
                self.trader.create_spxw_contract(trade.short_expiry, trade.put_strike, "P"),
                self.trader.create_spxw_contract(trade.short_expiry, trade.call_strike, "C"),
                self.trader.create_spxw_contract(trade.long_expiry, trade.put_strike, "P"),
                self.trader.create_spxw_contract(trade.long_expiry, trade.call_strike, "C")
            ]
            
            leg_names = [
                f"SHORT {trade.put_strike} PUT  {trade.short_expiry}",
                f"SHORT {trade.call_strike} CALL {trade.short_expiry}",
                f"LONG  {trade.put_strike} PUT  {trade.long_expiry}",
                f"LONG  {trade.call_strike} CALL {trade.long_expiry}"
            ]
            
            # Get market data for each leg
            for i, (contract, name) in enumerate(zip(contracts, leg_names)):
                req_id = self.trader.get_next_req_id()
                self.trader.client.reqMktData(req_id, contract, "106", False, False, [])  # Include Greeks
                time.sleep(0.2)
                
                # Wait a moment for data
                time.sleep(1)
                
                if req_id in self.trader.wrapper.market_data:
                    data = self.trader.wrapper.market_data[req_id]
                    bid = data.get('bid', 0)
                    ask = data.get('ask', 0)
                    mid = (bid + ask) / 2 if bid and ask else 0
                    delta = data.get('delta', 0)
                    
                    print(f"{i+1}. {name}")
                    print(f"    Bid: ${bid:.2f}  Ask: ${ask:.2f}  Mid: ${mid:.2f}")
                    if delta:
                        print(f"    Delta: {delta:.3f}")
                    print(f"    Position: {'SOLD' if i < 2 else 'BOUGHT'} {self.trader.config.position_size} contracts")
                    print()
                    
                    self.trader.client.cancelMktData(req_id)
                else:
                    print(f"{i+1}. {name}")
                    print(f"    ERROR: Could not get market data")
                    print()
            
            print("TIP: TIP: Consider closing legs individually if you see profitable opportunities")
            print("TIP: TIP: Be aware that legging out creates unhedged risk")
            print("TIP: TIP: Monitor delta exposure as you close individual legs")
            
        except Exception as e:
            print(f"ERROR: Error getting leg values: {e}")
    
    def interactive_menu(self):
        """Interactive menu for manual overrides"""
        while True:
            print("\n" + "="*50)
            print("SPX CALENDAR SPREAD MANUAL OVERRIDE MENU")
            print("="*50)
            print("1. List active positions")
            print("2. Close position by ID")
            print("3. Close position by number")
            print("4. Take over position manually (for legging out)")
            print("5. Close all positions")
            print("6. View trade history")
            print("7. System status")
            print("0. Exit")
            print("-"*50)
            
            try:
                choice = input("Enter your choice: ").strip()
                
                if choice == "0":
                    print("👋 Exiting manual override menu")
                    break
                
                elif choice == "1":
                    self.list_active_positions()
                
                elif choice == "2":
                    trade_id = input("Enter trade ID: ").strip()
                    reason = input("Enter reason (optional): ").strip()
                    if not reason:
                        reason = "Manual override"
                    self.force_close_position(trade_id, reason)
                
                elif choice == "3":
                    try:
                        pos_num = int(input("Enter position number: ").strip())
                        reason = input("Enter reason (optional): ").strip()
                        if not reason:
                            reason = "Manual override"
                        self.force_close_by_number(pos_num, reason)
                    except ValueError:
                        print("ERROR: Please enter a valid number")
                
                elif choice == "4":
                    try:
                        pos_num = int(input("Enter position number to take over: ").strip())
                        self.take_over_position(pos_num)
                    except ValueError:
                        print("ERROR: Please enter a valid number")
                
                elif choice == "5":
                    confirm = input("⚠️ Close ALL positions? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        reason = input("Enter reason: ").strip()
                        if not reason:
                            reason = "Manual override - close all"
                        self.close_all_positions(reason)
                    else:
                        print("ERROR: Operation cancelled")
                
                elif choice == "6":
                    self.view_trade_history()
                
                elif choice == "7":
                    self.show_system_status()
                
                else:
                    print("ERROR: Invalid choice. Please try again.")
                    
            except KeyboardInterrupt:
                print("\n👋 Exiting...")
                break
            except Exception as e:
                print(f"ERROR: Error: {e}")
    
    def close_all_positions(self, reason: str):
        """Close all active positions"""
        active_trades = self.trader.db.get_active_trades()
        
        if not active_trades:
            print("No active positions to close")
            return
        
        print(f" Closing {len(active_trades)} active positions...")
        
        success_count = 0
        for trade in active_trades:
            if self.force_close_position(trade.trade_id, reason):
                success_count += 1
        
        print(f"SUCCESS: Successfully closed {success_count}/{len(active_trades)} positions")
    
    def view_trade_history(self, limit: int = 10):
        """View recent trade history"""
        conn = sqlite3.connect(self.trader.db.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT trade_id, entry_date, spx_price, put_strike, call_strike, 
                   entry_credit, status, exit_reason, unrealized_pnl
            FROM calendar_trades 
            ORDER BY entry_date DESC, entry_time DESC 
            LIMIT ?
        ''', (limit,))
        
        trades = cursor.fetchall()
        conn.close()
        
        if not trades:
            print("No trade history found")
            return
        
        print(f"\n=== RECENT TRADE HISTORY (Last {len(trades)}) ===")
        for trade in trades:
            trade_id, entry_date, spx_price, put_strike, call_strike, entry_credit, status, exit_reason, pnl = trade
            print(f"ID: {trade_id}")
            print(f"Date: {entry_date}, SPX: ${spx_price:.2f}")
            print(f"Strikes: {put_strike}P/{call_strike}C, Credit: ${entry_credit:.2f}")
            print(f"Status: {status}" + (f" ({exit_reason})" if exit_reason else ""))
            if pnl is not None:
                pnl_pct = (pnl / abs(entry_credit) * 100) if entry_credit != 0 else 0
                print(f"P&L: ${pnl:.2f} ({pnl_pct:.1f}%)")
            print("-" * 40)
    
    def show_system_status(self):
        """Show system status information"""
        print("\n=== SYSTEM STATUS ===")
        print(f"IBKR Connected: {'SUCCESS:' if self.trader.is_connected else 'ERROR:'}")
        print(f"Database Path: {self.trader.config.db_path}")
        print(f"Max Positions: {self.trader.config.max_concurrent_positions}")
        print(f"Position Size: {self.trader.config.position_size} contracts")
        print(f"Target Delta: {self.trader.config.target_delta}")
        print(f"Profit Target: {self.trader.config.profit_target_pct * 100}%")
        print(f"Exit Day: {self.trader.config.exit_day}")
        
        # Active positions count
        active_trades = self.trader.db.get_active_trades()
        print(f"Active Positions: {len(active_trades)}/{self.trader.config.max_concurrent_positions}")
        
        # Today's activity
        today = datetime.now().strftime('%Y-%m-%d')
        today_count = self.trader.db.get_trade_count_for_date(today)
        print(f"Trades Today: {today_count}")
        
        # Recent log entries
        conn = sqlite3.connect(self.trader.db.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT action, message, success, timestamp 
            FROM daily_log 
            ORDER BY timestamp DESC 
            LIMIT 5
        ''')
        recent_logs = cursor.fetchall()
        conn.close()
        
        if recent_logs:
            print("\nRecent Activity:")
            for action, message, success, timestamp in recent_logs:
                status = "SUCCESS:" if success else "ERROR:"
                print(f"  {status} {timestamp}: {action} - {message}")

# ===============================================
# MAIN ENTRY POINT
# ===============================================

def main():
    """Main entry point"""
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='SPX Double Calendar Spread Trading System')
    parser.add_argument('--mode', choices=['auto', 'manual', 'test'], default='auto',
                       help='Operation mode: auto (scheduler), manual (override menu), test (single run)')
    parser.add_argument('--config-file', help='Path to configuration file')
    
    args = parser.parse_args()
    
    print(" SPX Double Calendar Spread Trading System")
    print("=" * 50)
    
    # Load configuration
    config = CalendarConfig()
    
    # Override config from file if provided
    if args.config_file:
        # TODO: Implement config file loading
        print(f"📄 Config file support not yet implemented: {args.config_file}")
    
    # Create trader instance
    trader = SPXCalendarTrader(config)
    
    # Create manual override interface
    override = ManualOverride(trader)
    
    try:
        if args.mode == 'manual':
            print("🎛️ Starting manual override interface...")
            override.interactive_menu()
            
        elif args.mode == 'test':
            print("🧪 Running test execution...")
            if trader.connect_to_ibkr():
                trader.daily_trading_routine()
                trader.disconnect_from_ibkr()
            else:
                print("ERROR: Could not connect to IBKR for test")
                
        else:  # auto mode
            print("⏰ Starting automated scheduler...")
            print("Will execute daily at 9:45 AM ET (M-F)")
            print("Will check for time-based exits daily at 3:00 PM ET (M-F)")
            print("Will reconcile positions daily at 5:00 PM ET (M-F)")
            print("TIP: Press Ctrl+C to stop, or use --mode manual for override menu")
            
            # Start the scheduler (this will run indefinitely)
            trader.start_scheduler()
        
    except KeyboardInterrupt:
        print("\n👋 Shutting down trading system...")
        trader.disconnect_from_ibkr()
        print("SUCCESS: System shutdown complete")

if __name__ == "__main__":
    main()
