#!/usr/bin/env python3
"""
SPX Double Calendar Spread - Web Management Interface
Simple Flask app for managing the trading system via browser
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import sqlite3
import json
from datetime import datetime, timedelta
import threading
import time
import schedule
import pytz
from spx_double_calendar import SPXCalendarTrader, CalendarConfig, ManualOverride

app = Flask(__name__)
app.secret_key = 'spx_calendar_trading_system_2024'

# Add abs function to Jinja2 globals
app.jinja_env.globals['abs'] = abs

# Global trader instance
trader = None
config = None
scheduler_thread = None
scheduler_running = False
last_reconnect_attempt = 0

def init_trader():
    """Initialize the trading system with IBKR connection"""
    global trader, config
    config = CalendarConfig()
    # Use a different client ID for the web application to avoid conflicts
    config.ib_client_id = 10  # Use ID 10 to avoid conflicts with main system (2)
    trader = SPXCalendarTrader(config)
    
    # Connect to IBKR for quotes and position management
    print("🔌 Connecting to IBKR for web interface...")
    success = trader.connect_to_ibkr()
    if success:
        print("✅ Web interface connected to IBKR")
        # Start streaming for quotes and positions
        try:
            trader.start_spx_streaming()
            # Start streaming for active positions
            active_trades = trader.db.get_active_trades()
            for trade in active_trades:
                if trade.status == "ACTIVE":
                    trader.start_position_streaming(trade)
            print("✅ Streaming started for web interface")
        except Exception as e:
            print(f"⚠️ Streaming failed: {e}")
    else:
        print("❌ Web interface failed to connect to IBKR")

# Web interface maintains its own IBKR connection for live pricing and immediate exits
# Uses Client ID 10 to avoid conflicts with main system (Client ID 2)

def connect_to_ibkr():
    """Connect web interface to IBKR with proper error handling"""
    if not trader:
        return False
        
    try:
        if trader.is_connected:
            return True
            
        # Attempt connection with retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                success = trader.connect_to_ibkr()
                if success:
                    # Start streaming for active positions
                    trader.start_streaming_for_active_positions()
                    return True
                else:
                    print(f"Connection attempt {attempt + 1} failed")
                    if attempt < max_retries - 1:
                        time.sleep(2)  # Wait before retry
            except Exception as e:
                print(f"Connection attempt {attempt + 1} error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    
        return False
        
    except Exception as e:
        print(f"Error in connect_to_ibkr: {e}")
        return False

@app.route('/')
def dashboard():
    """Main dashboard showing system status and active positions"""
    if not trader:
        init_trader()
    
    # Get active positions
    active_trades = trader.db.get_active_trades()
    
    # Enhance P&L display calculations for each trade
    for trade in active_trades:
        # Always initialize these attributes first
        trade.pnl_per_contract = 0.0
        trade.pnl_total = 0.0
        trade.current_spread_price = 0.0
        trade.current_value = 0.0  # For template compatibility
        trade.unrealized_pnl = 0.0  # For template compatibility
        
        # Ensure all numeric fields are valid floats for template formatting
        trade.spx_price = float(trade.spx_price) if trade.spx_price else 0.0
        trade.put_strike = float(trade.put_strike) if trade.put_strike else 0.0
        trade.call_strike = float(trade.call_strike) if trade.call_strike else 0.0
        trade.entry_credit = float(trade.entry_credit) if trade.entry_credit else 0.0
        trade.profit_target = float(trade.profit_target) if trade.profit_target else 0.0
        
        if trade.status == "ACTIVE":
            # Try to get real-time streaming P&L first
            streaming_result = trader.get_streaming_pnl(trade.trade_id)
            
            if streaming_result:
                # Use real-time streaming data
                current_spread_price, unrealized_pnl = streaming_result
                trade.current_value = current_spread_price
                trade.unrealized_pnl = unrealized_pnl
            
            # Calculate display values
            if trade.unrealized_pnl != 0:
                try:
                    # SPX options have 100x multiplier, and we have position_size contracts
                    trade.pnl_per_contract = float(trade.unrealized_pnl) * 100  # Per contract dollar amount
                    trade.pnl_total = trade.pnl_per_contract * config.position_size  # Total position
                except (ValueError, TypeError):
                    trade.pnl_per_contract = 0.0
                    trade.pnl_total = 0.0
            
            # Set current spread price for display
            if trade.current_value is not None:
                try:
                    trade.current_spread_price = float(trade.current_value)
                except (ValueError, TypeError):
                    trade.current_spread_price = 0.0
        
        # Calculate days since entry for all trades (needed for manual control reminders)
        try:
            entry_date = datetime.strptime(trade.entry_date, '%Y-%m-%d')
            days_since_entry = (datetime.now() - entry_date).days
            trade.days_since_entry = days_since_entry
        except:
            trade.days_since_entry = 0
        
        # Format expiration dates for display
        try:
            # Convert YYYYMMDD format to MM/DD for display
            short_expiry_date = datetime.strptime(trade.short_expiry, '%Y%m%d')
            trade.short_expiry_display = short_expiry_date.strftime('%m/%d')
            
            long_expiry_date = datetime.strptime(trade.long_expiry, '%Y%m%d')
            trade.long_expiry_display = long_expiry_date.strftime('%m/%d')
        except:
            trade.short_expiry_display = "N/A"
            trade.long_expiry_display = "N/A"
        
        # Calculate exit date (14 days from entry or configured exit day)
        try:
            exit_date = entry_date + timedelta(days=config.exit_day)
            trade.exit_date_display = exit_date.strftime('%m/%d')
        except:
            trade.exit_date_display = "N/A"
    
    # Calculate total P&L across all active positions
    total_pnl = 0.0
    for trade in active_trades:
        if trade.status == "ACTIVE" and hasattr(trade, 'pnl_total') and trade.pnl_total:
            total_pnl += trade.pnl_total
    
    # Import timezone handling
    from datetime import datetime as dt_parser
    
    # Get today's activity
    today = datetime.now().strftime('%Y-%m-%d')
    today_count = trader.db.get_trade_count_for_date(today)
    
    # Get recent log entries with timezone conversion
    conn = sqlite3.connect(trader.db.db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT action, message, success, timestamp 
        FROM daily_log 
        ORDER BY timestamp DESC 
        LIMIT 10
    ''')
    raw_logs = cursor.fetchall()
    conn.close()
    
    # Convert timestamps to Eastern Time for display
    recent_logs = []
    eastern_tz = pytz.timezone('US/Eastern')
    
    for action, message, success, timestamp in raw_logs:
        try:
            # All old database timestamps are in UTC, convert to Eastern Time
            if isinstance(timestamp, str):
                # Parse string timestamp and treat as UTC
                dt_naive = dt_parser.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                dt_utc = pytz.UTC.localize(dt_naive)
            else:
                # Handle datetime objects - assume UTC if no timezone
                dt_utc = pytz.UTC.localize(timestamp) if timestamp.tzinfo is None else timestamp
            
            # Convert UTC to Eastern Time  
            et_time = dt_utc.astimezone(eastern_tz)
            formatted_time = et_time.strftime('%Y-%m-%d %H:%M:%S')
            
            recent_logs.append((action, message, success, formatted_time))
        except Exception as e:
            # If conversion fails, use original timestamp
            print(f"Timestamp conversion error for '{timestamp}': {e}")
            recent_logs.append((action, message, success, str(timestamp)))
    
    # Get current SPX price
    spx_price = None
    spx_change = None
    spx_change_pct = None
    if trader and trader.is_connected:
        try:
            spx_price = trader.get_spx_price()
            # For now, we'll just show the price. Later we can add change calculation
            spx_change = 0.0  # Placeholder for daily change
            spx_change_pct = 0.0  # Placeholder for daily change %
        except Exception as e:
            print(f"Error getting SPX price: {e}")
    
    # System status
    system_status = {
        'connected': trader.is_connected if trader else False,
        'scheduler_running': False,  # Web manager doesn't run scheduler
        'active_positions': len(active_trades),
        'max_positions': config.max_concurrent_positions,
        'trades_today': today_count,
        'target_delta': config.target_delta,
        'position_size': config.position_size,
        'profit_target': f"{config.profit_target_pct * 100}%",
        'spx_price': spx_price,
        'spx_change': spx_change,
        'spx_change_pct': spx_change_pct
    }
    
    return render_template('dashboard.html', 
                         trades=active_trades,  # Pass as 'trades' for template consistency
                         active_trades=active_trades,  # Keep for backwards compatibility
                         system_status=system_status,
                         recent_logs=recent_logs,
                         total_pnl=total_pnl)

@app.route('/positions')
def positions():
    """View all positions with detailed information"""
    if not trader:
        init_trader()
    
    active_trades = trader.db.get_active_trades()
    
    # Calculate additional metrics for each trade
    for trade in active_trades:
        # Initialize attributes for template compatibility (same as dashboard)
        trade.pnl_per_contract = 0.0
        trade.pnl_total = 0.0
        trade.current_spread_price = 0.0
        trade.current_value = 0.0
        trade.unrealized_pnl = 0.0
        
        # Ensure all numeric fields are valid floats
        trade.spx_price = float(trade.spx_price) if trade.spx_price else 0.0
        trade.put_strike = float(trade.put_strike) if trade.put_strike else 0.0
        trade.call_strike = float(trade.call_strike) if trade.call_strike else 0.0
        trade.entry_credit = float(trade.entry_credit) if trade.entry_credit else 0.0
        trade.profit_target = float(trade.profit_target) if trade.profit_target else 0.0
        
        if trade.status == "ACTIVE":
            # Try to get real-time streaming P&L
            streaming_result = trader.get_streaming_pnl(trade.trade_id)
            if streaming_result:
                current_spread_price, unrealized_pnl = streaming_result
                trade.current_value = current_spread_price
                trade.unrealized_pnl = unrealized_pnl
                
                # Calculate display values
                if trade.unrealized_pnl != 0:
                    trade.pnl_per_contract = float(trade.unrealized_pnl) * 100
                    trade.pnl_total = trade.pnl_per_contract * config.position_size
                
                if trade.current_value is not None:
                    trade.current_spread_price = float(trade.current_value)
        
        # Calculate days since entry
        entry_date = datetime.strptime(trade.entry_date, '%Y-%m-%d')
        days_since_entry = (datetime.now() - entry_date).days
        trade.days_since_entry = days_since_entry
        
        # Format expiration dates for display (same as dashboard)
        try:
            short_expiry_date = datetime.strptime(trade.short_expiry, '%Y%m%d')
            trade.short_expiry_display = short_expiry_date.strftime('%m/%d')
            
            long_expiry_date = datetime.strptime(trade.long_expiry, '%Y%m%d')
            trade.long_expiry_display = long_expiry_date.strftime('%m/%d')
        except:
            trade.short_expiry_display = "N/A"
            trade.long_expiry_display = "N/A"
        
        # Calculate exit date
        try:
            exit_date = entry_date + timedelta(days=config.exit_day)
            trade.exit_date_display = exit_date.strftime('%m/%d')
        except:
            trade.exit_date_display = "N/A"
        
        # Calculate days to short expiry
        try:
            expiry_date = datetime.strptime(trade.short_expiry, '%Y%m%d')
            days_to_expiry = (expiry_date - datetime.now()).days
            trade.days_to_expiry = days_to_expiry
        except:
            trade.days_to_expiry = 0
        
        # Calculate profit percentage
        if trade.entry_credit != 0:
            trade.profit_pct = (trade.unrealized_pnl / abs(trade.entry_credit)) * 100
        else:
            trade.profit_pct = 0
    
    return render_template('positions.html', trades=active_trades)

@app.route('/close_position/<trade_id>', methods=['POST'])
def close_position(trade_id):
    """Close a specific position"""
    if not trader:
        init_trader()
    
    try:
        # Get the trade
        trade = trader.db.get_trade_by_id(trade_id)
        if not trade:
            flash(f'Trade {trade_id} not found', 'error')
            return redirect(url_for('positions'))
        
        # Close the position using the proper trading engine method
        debug_msg = f"🔍 DEBUG: Attempting to close position {trade_id}"
        print(debug_msg)
        
        debug_msg = f"🔍 DEBUG: Trade details - Short: {trade.put_strike}P/{trade.call_strike}C, Long: {trade.long_put_strike}P/{trade.long_call_strike}C"
        print(debug_msg)
        
        debug_msg = f"🔍 DEBUG: IBKR connection status: {trader.is_connected}"
        print(debug_msg)
        
        # Also write to log file for debugging
        with open('web_debug.log', 'a') as f:
            from datetime import datetime
            eastern_tz = pytz.timezone('US/Eastern')
            eastern_time = datetime.now(eastern_tz)
            f.write(f"{eastern_time.strftime('%Y-%m-%d %H:%M:%S')} - Attempting to close {trade_id}\n")
            f.write(f"  Trade: Short {trade.put_strike}P/{trade.call_strike}C, Long {trade.long_put_strike}P/{trade.long_call_strike}C\n")
            f.write(f"  IBKR Connected: {trader.is_connected}\n")
        
        # Ensure IBKR connection
        if not connect_to_ibkr():
            flash('❌ Cannot close position - not connected to IBKR', 'error')
            return redirect(url_for('positions'))
        
        # Close the position using the main trading system
        success = trader.close_calendar_position(trade, f"Manual close via web interface")
        
        if success:
            flash(f'✅ Position {trade_id} closed successfully', 'success')
            print(f"✅ Successfully closed {trade_id} via web interface")
        else:
            flash(f'❌ Failed to close position {trade_id}. Check logs for details.', 'error')
            print(f"❌ Failed to close {trade_id}")
        
    except Exception as e:
        flash(f'Error closing position: {str(e)}', 'error')
    
    return redirect(url_for('positions'))

@app.route('/stop_managing/<trade_id>', methods=['POST'])
def stop_managing(trade_id):
    """Stop system management of a position"""
    if not trader:
        init_trader()

    try:
        # Get the trade
        trade = trader.db.get_trade_by_id(trade_id)
        if not trade:
            flash(f'Trade {trade_id} not found', 'error')
            return redirect(url_for('positions'))

        # Add command to queue for main system to process
        command_id = trader.db.add_command('STOP_MANAGING', trade_id)
        
        flash(f'✅ Stop managing command queued for {trade_id}. The main system will process it shortly.', 'info')
        print(f"✅ Stop managing command queued for {trade_id} (Command ID: {command_id})")

    except Exception as e:
        flash(f'Error stopping management: {str(e)}', 'error')

    return redirect(url_for('positions'))

@app.route('/record_manual_close/<trade_id>', methods=['POST'])
def record_manual_close(trade_id):
    """Record manual close of a position"""
    if not trader:
        init_trader()

    try:
        # Get the trade
        trade = trader.db.get_trade_by_id(trade_id)
        if not trade:
            flash(f'Trade {trade_id} not found', 'error')
            return redirect(url_for('positions'))

        if trade.status != "MANUAL_CONTROL":
            flash(f'Trade {trade_id} is not under manual control', 'error')
            return redirect(url_for('positions'))

        # Get form data
        exit_price = float(request.form.get('exit_price', 0))
        exit_date = request.form.get('exit_date', '')
        exit_time = request.form.get('exit_time', '')
        
        if not exit_price or not exit_date or not exit_time:
            flash('All fields are required', 'error')
            return redirect(url_for('positions'))

        # Calculate P&L
        pnl = exit_price - trade.entry_credit
        
        # Update trade record
        trade.status = "CLOSED"
        trade.exit_reason = "Manual close recorded"
        trade.exit_date = exit_date
        trade.exit_time = exit_time
        trade.exit_credit = exit_price
        trade.realized_pnl = pnl
        
        # Save the updated trade
        trader.db.save_trade(trade)
        
        # Log the action
        trader.db.log_daily_action(
            'MANUAL_CLOSE_RECORDED',
            f'Manual close recorded for {trade_id}: ${exit_price:.2f}, P&L: ${pnl:.2f}',
            True
        )
        
        # Send notification
        trader.notifications.send_sms(f"SPX Calendar: Manual close recorded for {trade_id}. Exit: ${exit_price:.2f}, P&L: ${pnl:.2f}")
        
        flash(f'Manual close recorded for {trade_id}. P&L: ${pnl:.2f}', 'success')

    except ValueError:
        flash('Invalid exit price format', 'error')
    except Exception as e:
        flash(f'Error recording manual close: {str(e)}', 'error')

    return redirect(url_for('positions'))

@app.route('/restart_streaming')
def restart_streaming():
    """Restart streaming for all active positions"""
    if not trader:
        init_trader()
    
    try:
        # Stop existing streaming
        stop_streaming_data()
        time.sleep(1)
        
        # Restart streaming
        start_streaming_data()
        
        flash('✅ Streaming restarted for all active positions', 'success')
    except Exception as e:
        flash(f'❌ Error restarting streaming: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/fix_new_position')
def fix_new_position():
    """Position fixing disabled - web manager is read-only"""
    flash('❌ Position fixing disabled in web interface. Main trading system handles GTC orders and streaming.', 'error')
    return redirect(url_for('dashboard'))

@app.route('/debug_gtc_orders')
def debug_gtc_orders():
    """Debug GTC profit target orders and optionally place missing ones"""
    if not trader:
        init_trader()
        flash('Trader not initialized', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        # Get active trades 
        active_trades = trader.db.get_active_trades()
        if not active_trades:
            flash('No active trades found', 'info')
            return redirect(url_for('dashboard'))
        
        # Separate trades with and without GTC orders
        gtc_trades = [t for t in active_trades if t.profit_target_order_id > 0]
        missing_gtc = [t for t in active_trades if t.status == "ACTIVE" and (t.profit_target_order_id == 0 or t.profit_target_status == "NONE")]
        
        # Check status of existing GTC orders
        if gtc_trades:
            # Request fresh order status from IBKR
            trader.request_open_orders()
            
            # Check GTC order status
            trader.check_gtc_order_status()
            
            gtc_info = []
            for trade in gtc_trades:
                gtc_info.append(f"{trade.trade_id}: Order {trade.profit_target_order_id} ({trade.profit_target_status}) @ ${trade.profit_target_price:.2f}")
            
            flash(f'✅ Found {len(gtc_trades)} existing GTC orders: {"; ".join(gtc_info)}', 'info')
        
        # Report missing GTC orders
        if missing_gtc:
            missing_info = [f"{t.trade_id} (${t.entry_credit:.2f})" for t in missing_gtc]
            flash(f'⚠️ Found {len(missing_gtc)} trades missing GTC orders: {", ".join(missing_info)}', 'warning')
            
            # Check if user wants to place missing GTC orders
            place_missing = request.args.get('place_missing', 'false').lower() == 'true'
            if place_missing:
                # Attempt to place missing GTC orders
                result = trader.place_missing_gtc_orders()
                if result['success']:
                    flash(result['message'], 'success')
                else:
                    flash(result['message'], 'error')
            else:
                # Show option to place missing orders
                flash('💡 Click "Place Missing GTC Orders" to automatically place the missing profit target orders', 'info')
        else:
            if gtc_trades:
                flash('✅ All active trades have GTC profit target orders', 'success')
        
    except Exception as e:
        flash(f'Error checking GTC orders: {e}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/place_missing_gtc_orders')
def place_missing_gtc_orders():
    """Place missing GTC orders directly via web interface IBKR connection"""
    if not trader:
        init_trader()
        flash('Trader not initialized', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        # Ensure IBKR connection
        if not connect_to_ibkr():
            flash('❌ Cannot place GTC orders - not connected to IBKR', 'error')
            return redirect(url_for('dashboard'))
        
        # Attempt to place missing GTC orders directly
        result = trader.place_missing_gtc_orders()
        if result['success']:
            flash(f"✅ {result['message']}", 'success')
            # Log the action
            trader.db.log_daily_action(
                'PLACE_MISSING_GTC',
                f"Placed {result['placed']} missing GTC orders via web interface",
                True
            )
        else:
            flash(f"❌ {result['message']}", 'error')
            
    except Exception as e:
        flash(f'Error placing missing GTC orders: {e}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/repair_gtc_orders')
def repair_gtc_orders():
    """GTC repair disabled - web manager is read-only"""
    flash('❌ GTC order repair disabled in web interface. Main trading system handles GTC orders.', 'error')
    return redirect(url_for('dashboard'))

@app.route('/debug_trades')
def debug_trades():
    """Debug all trades in database to check manual control reminders"""
    if not trader:
        init_trader()
    
    try:
        # Get ALL trades from database regardless of status
        conn = sqlite3.connect(trader.db.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT trade_id, status, entry_date, exit_date 
            FROM calendar_trades 
            ORDER BY entry_date DESC
            LIMIT 20
        ''')
        all_trades = cursor.fetchall()
        conn.close()
        
        # Get active trades (what the app uses)
        active_trades = trader.db.get_active_trades()
        
        debug_info = f"<h3>All Trades in DB:</h3><ul>"
        for trade in all_trades:
            debug_info += f"<li><strong>{trade[0]}</strong> - Status: {trade[1]} - Entry: {trade[2]} - Exit: {trade[3] or 'None'}</li>"
        debug_info += f"</ul><h3>Active Trades Retrieved:</h3><ul>"
        for trade in active_trades:
            debug_info += f"<li><strong>{trade.trade_id}</strong> - Status: {trade.status} - Entry: {trade.entry_date}</li>"
        debug_info += "</ul>"
        
        flash(f'Debug info: {len(all_trades)} total trades, {len(active_trades)} active trades', 'info')
        print(f"DEBUG TRADES:\nAll trades: {len(all_trades)}\nActive trades: {len(active_trades)}")
        for trade in active_trades:
            print(f"  {trade.trade_id}: {trade.status}")
        
    except Exception as e:
        flash(f'Error debugging trades: {e}', 'error')
        print(f"Error in debug_trades: {e}")
    
    return redirect(url_for('dashboard'))

@app.route('/system')
def system_management():
    """Combined system management page with settings and status"""
    if not trader:
        init_trader()
    
    # Check if main trading system is running and IBKR connection status
    main_system_running = False
    ibkr_connected = False
    
    try:
        import os
        import time
        
        # Check if main system is active (log file activity)
        log_file = 'spx_calendar.log'
        if os.path.exists(log_file):
            last_modified = os.path.getmtime(log_file)
            current_time = time.time()
            if current_time - last_modified < 300:  # 5 minutes
                main_system_running = True
                
                # If main system is running, check for IBKR connection in recent logs
                try:
                    with open(log_file, 'r', encoding='utf-8') as f:
                        # Read last 50 lines to check for connection status
                        lines = f.readlines()[-50:]
                        for line in reversed(lines):
                            if 'Connected to Interactive Brokers' in line:
                                ibkr_connected = True
                                break
                            elif 'Disconnected from Interactive Brokers' in line or 'Connection error' in line:
                                ibkr_connected = False
                                break
                except Exception as log_error:
                    print(f"Error reading log for IBKR status: {log_error}")
    except Exception as e:
        print(f"Error checking main system status: {e}")
    
    # Get system information  
    system_info = {
        'connected': trader.is_connected if trader else False,
        'scheduler_running': main_system_running,
        'host': config.ib_host,
        'port': config.ib_port,
        'client_id': config.ib_client_id,
        'active_positions': len(trader.db.get_active_trades()) if trader else 0,
        'max_positions': config.max_concurrent_positions,
        'position_size': config.position_size,
        'trades_today': trader.db.get_trade_count_for_date(datetime.now().strftime('%Y-%m-%d')) if trader else 0,
        'spx_price': trader.current_spx_price if trader and hasattr(trader, 'current_spx_price') else None,
        'db_path': config.db_path,
        'total_trades': trader.db.get_total_trade_count() if trader else 0,
        'last_update': datetime.now().strftime('%H:%M:%S')
    }
    
    # Get all settings organized by category
    settings_by_category = trader.db.get_all_settings()
    
    return render_template('system_combined.html', 
                         system_status=system_info, 
                         settings_by_category=settings_by_category)


@app.route('/connect_ibkr', methods=['POST'])
def connect_ibkr():
    """Connect to Interactive Brokers"""
    if not trader:
        init_trader()
    
    try:
        if not trader.is_connected:
            success = trader.connect_to_ibkr()
            if success:
                flash('Connected to Interactive Brokers successfully', 'success')
            else:
                flash('Failed to connect to Interactive Brokers', 'error')
        else:
            flash('Already connected to Interactive Brokers', 'info')
    except Exception as e:
        flash(f'Connection error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/disconnect_ibkr', methods=['POST'])
def disconnect_ibkr():
    """Disconnect from IBKR"""
    if not trader:
        init_trader()
    
    try:
        trader.disconnect_from_ibkr()
        flash('Disconnected from IBKR', 'success')
    except Exception as e:
        flash(f'Disconnection error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/run_reconciliation', methods=['POST'])
def run_reconciliation():
    """Manually trigger position reconciliation"""
    if not trader:
        init_trader()
    
    try:
        # Add command to queue for main system to process
        command_id = trader.db.add_command('RUN_RECONCILIATION')
        flash('✅ Reconciliation command queued. The main system will process it shortly.', 'info')
        print(f"✅ Manual reconciliation command queued (Command ID: {command_id})")
        
    except Exception as e:
        flash(f'Error queuing reconciliation: {str(e)}', 'error')
        print(f"❌ Failed to queue reconciliation: {e}")
    
    return redirect(url_for('system_management'))



@app.route('/get_pnl_data')
def get_pnl_data():
    """Get current P&L data from streaming market data (real-time)"""
    if not trader:
        init_trader()
    
    try:
        active_trades = trader.db.get_active_trades()
        pnl_data = []
        
        for trade in active_trades:
            if trade.status == "ACTIVE":
                # Try to get real-time streaming P&L first
                streaming_result = trader.get_streaming_pnl(trade.trade_id)
                
                if streaming_result:
                    # Use real-time streaming data
                    current_spread_price, unrealized_pnl = streaming_result
                else:
                    # Fall back to stored database values
                    current_spread_price = trade.current_value if hasattr(trade, 'current_value') and trade.current_value else 0.0
                    unrealized_pnl = trade.unrealized_pnl if hasattr(trade, 'unrealized_pnl') and trade.unrealized_pnl else 0.0
                
                # Calculate display values
                pnl_per_contract = 0.0
                pnl_total = 0.0
                pnl_percentage = 0.0
                
                if unrealized_pnl != 0:
                    pnl_per_contract = float(unrealized_pnl) * 100
                    pnl_total = pnl_per_contract * config.position_size
                    pnl_percentage = (unrealized_pnl / abs(trade.entry_credit)) * 100
                
                pnl_data.append({
                    'trade_id': trade.trade_id,
                    'pnl_total': pnl_total,
                    'pnl_per_contract': pnl_per_contract,
                    'current_spread_price': current_spread_price,
                    'unrealized_pnl': unrealized_pnl,
                    'pnl_percentage': pnl_percentage,
                    'has_pnl': unrealized_pnl != 0,
                    'is_streaming': streaming_result is not None
                })
        
        # Also get current SPX price from streaming
        spx_price = 0.0
        if trader.spx_stream_req_id and trader.spx_stream_req_id in trader.wrapper.streaming_data:
            spx_data = trader.wrapper.streaming_data[trader.spx_stream_req_id]
            spx_price = spx_data.get('price', trader.current_spx_price)
        else:
            spx_price = trader.current_spx_price
        
        return jsonify({
            'success': True,
            'pnl_data': pnl_data,
            'spx_price': spx_price,
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'streaming_active': len(trader.streaming_positions) > 0 if trader else False
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/start_scheduler', methods=['POST'])
def start_scheduler_route():
    """Start the automated scheduler"""
    try:
        start_scheduler()
        flash('Automated scheduler started successfully', 'success')
    except Exception as e:
        flash(f'Error starting scheduler: {str(e)}', 'error')
    
    return redirect(url_for('system_status'))

@app.route('/stop_scheduler', methods=['POST'])
def stop_scheduler_route():
    """Stop the automated scheduler"""
    try:
        stop_scheduler()
        flash('Automated scheduler stopped', 'success')
    except Exception as e:
        flash(f'Error stopping scheduler: {str(e)}', 'error')
    
    return redirect(url_for('system_status'))

@app.route('/manual_trade', methods=['GET', 'POST'])
def manual_trade():
    """Manual trade execution page"""
    if not trader:
        init_trader()
    
    if request.method == 'POST':
        try:
            # CRITICAL: Log manual trade request
            import datetime
            print(f"MANUAL TRADE REQUESTED at {datetime.datetime.now()}")
            print(f"Request IP: {request.remote_addr}")
            print(f"User Agent: {request.headers.get('User-Agent')}")
            
            # Execute manual trade
            trader.execute_calendar_spread_entry(is_manual=True)
            flash('Manual trade execution initiated', 'success')
        except Exception as e:
            flash(f'Manual trade error: {str(e)}', 'error')
        
        return redirect(url_for('dashboard'))
    
    return render_template('manual_trade.html')

@app.route('/import_trade', methods=['GET', 'POST'])
def import_trade():
    """Import a manually executed trade for system management"""
    if not trader:
        init_trader()
    
    if request.method == 'POST':
        try:
            # Get form data
            entry_date = request.form.get('entry_date')
            entry_time = request.form.get('entry_time', '09:45:00')
            put_strike = float(request.form.get('put_strike'))
            call_strike = float(request.form.get('call_strike'))
            short_expiry = request.form.get('short_expiry')
            long_expiry = request.form.get('long_expiry')
            entry_price = float(request.form.get('entry_price'))
            spx_price = float(request.form.get('spx_price', 0))
            
            # Validate required fields
            if not all([entry_date, put_strike, call_strike, short_expiry, long_expiry, entry_price]):
                flash('All fields are required', 'error')
                return render_template('import_trade.html')
            
            # Convert date formats
            try:
                # Convert MM/DD/YYYY to YYYYMMDD for expiries
                short_exp_date = datetime.strptime(short_expiry, '%m/%d/%Y')
                long_exp_date = datetime.strptime(long_expiry, '%m/%d/%Y')
                short_expiry_formatted = short_exp_date.strftime('%Y%m%d')
                long_expiry_formatted = long_exp_date.strftime('%Y%m%d')
                
                # Validate entry date format
                entry_date_obj = datetime.strptime(entry_date, '%Y-%m-%d')
                
            except ValueError as e:
                flash(f'Invalid date format: {e}', 'error')
                return render_template('import_trade.html')
            
            # Create trade ID
            trade_id = f"CAL_{entry_date.replace('-', '')}_IMPORT"
            
            # Check if trade already exists
            existing_trade = trader.db.get_trade_by_id(trade_id)
            if existing_trade:
                flash(f'Trade {trade_id} already exists', 'error')
                return render_template('import_trade.html')
            
            # Create CalendarSpread object
            from spx_double_calendar import CalendarSpread
            
            trade = CalendarSpread(
                trade_id=trade_id,
                entry_date=entry_date,
                entry_time=entry_time,
                spx_price=spx_price if spx_price > 0 else trader.get_spx_price(),
                short_expiry=short_expiry_formatted,
                long_expiry=long_expiry_formatted,
                put_strike=put_strike,
                call_strike=call_strike,
                long_put_strike=put_strike,  # Assume same strikes initially
                long_call_strike=call_strike,
                entry_credit=entry_price,
                status="ACTIVE",
                fill_status="FILLED"
            )
            
            # Calculate profit target
            trade.profit_target = entry_price + (entry_price * config.profit_target_pct)
            
            # Save to database
            trader.db.save_trade(trade)
            
            # Log the import
            trader.db.log_daily_action(
                'TRADE_IMPORTED',
                f'Manual trade imported: {trade_id} at ${entry_price:.2f}',
                True
            )
            
            # Start streaming for the imported position
            try:
                trader.start_position_streaming(trade)
                print(f"✅ Started streaming for imported trade {trade_id}")
            except Exception as stream_error:
                print(f"⚠️ Failed to start streaming for {trade_id}: {stream_error}")
            
            # Try to place GTC profit target order (but expect it to fail for imported trades)
            try:
                success = trader.place_profit_target_order(trade)
                if success:
                    print(f"✅ GTC profit target order placed for imported trade {trade_id}")
                    flash(f'✅ GTC profit target order placed successfully', 'success')
                else:
                    print(f"⚠️ Failed to place GTC order for {trade_id} - this is normal for imported trades")
                    flash(f'⚠️ GTC order failed (normal for imported trades) - system will still monitor for manual exit', 'warning')
            except Exception as gtc_error:
                print(f"⚠️ GTC order error for {trade_id}: {gtc_error}")
                flash(f'⚠️ GTC order failed (normal for imported trades) - system will still monitor for manual exit', 'warning')
            
            # Send notification
            trader.notifications.send_sms(
                f"SPX Calendar: Imported trade {trade_id}. Entry: ${entry_price:.2f}, "
                f"Target: ${trade.profit_target:.2f}. Now under system management."
            )
            
            flash(f'✅ Trade {trade_id} imported successfully! System will now manage this position.', 'success')
            return redirect(url_for('dashboard'))
            
        except ValueError as e:
            flash(f'Invalid number format: {e}', 'error')
        except Exception as e:
            flash(f'Error importing trade: {e}', 'error')
            print(f"Import trade error: {e}")
    
    return render_template('import_trade.html')

@app.route('/history')
def trade_history():
    """View trade history"""
    if not trader:
        init_trader()
    
    # Get trade history with all needed fields
    conn = sqlite3.connect(trader.db.db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT trade_id, entry_date, entry_time, spx_price, put_strike, call_strike, 
               entry_credit, status, exit_reason, exit_date, exit_time, exit_spx_price, 
               exit_credit, realized_pnl, short_expiry, long_expiry
        FROM calendar_trades 
        ORDER BY entry_date DESC, entry_time DESC 
        LIMIT 50
    ''')
    trades = cursor.fetchall()
    conn.close()
    
    # Convert to list of dicts for easier template handling
    trade_list = []
    for trade in trades:
        trade_dict = {
            'trade_id': trade[0],
            'entry_date': trade[1],
            'entry_time': trade[2],
            'spx_price': trade[3],
            'put_strike': trade[4],
            'call_strike': trade[5],
            'entry_credit': trade[6],
            'status': trade[7],
            'exit_reason': trade[8],
            'exit_date': trade[9],
            'exit_time': trade[10],
            'exit_spx_price': trade[11],
            'exit_credit': trade[12],
            'pnl': trade[13],
            'short_expiry': trade[14],
            'long_expiry': trade[15]
        }
        
        # Format expiration dates for display
        try:
            if trade_dict['short_expiry']:
                short_expiry_date = datetime.strptime(trade_dict['short_expiry'], '%Y%m%d')
                trade_dict['short_expiry_display'] = short_expiry_date.strftime('%m/%d')
            else:
                trade_dict['short_expiry_display'] = "N/A"
                
            if trade_dict['long_expiry']:
                long_expiry_date = datetime.strptime(trade_dict['long_expiry'], '%Y%m%d')
                trade_dict['long_expiry_display'] = long_expiry_date.strftime('%m/%d')
            else:
                trade_dict['long_expiry_display'] = "N/A"
        except:
            trade_dict['short_expiry_display'] = "N/A"
            trade_dict['long_expiry_display'] = "N/A"
        
        # Calculate actual P&L (multiply by position size and SPX multiplier of 100)
        # SPX options have a multiplier of 100, so per-contract P&L * 100 * position_size
        if trade_dict['pnl'] and config.position_size:
            trade_dict['actual_pnl'] = trade_dict['pnl'] * config.position_size * 100
        else:
            trade_dict['actual_pnl'] = (trade_dict['pnl'] * 100) if trade_dict['pnl'] else 0
        
        # Calculate profit percentage
        if trade_dict['entry_credit'] and trade_dict['entry_credit'] != 0:
            trade_dict['profit_pct'] = (trade_dict['pnl'] / abs(trade_dict['entry_credit'])) * 100 if trade_dict['pnl'] else 0
        else:
            trade_dict['profit_pct'] = 0
            
        trade_list.append(trade_dict)
    
    return render_template('history.html', trades=trade_list)

# Legacy routes for backwards compatibility
@app.route('/settings')
def settings():
    """Redirect to combined system page"""
    return redirect(url_for('system_management'))

@app.route('/system_status')
def system_status():
    """Redirect to combined system page"""
    return redirect(url_for('system_management'))

@app.route('/update_settings', methods=['POST'])
def update_settings():
    """Update system settings"""
    if not trader:
        init_trader()
    
    try:
        # Get form data
        for setting_name in request.form:
            if setting_name.startswith('setting_'):
                # Remove 'setting_' prefix
                actual_name = setting_name[8:]
                new_value = request.form[setting_name]
                
                # Get the current setting to determine type
                current_settings = trader.db.get_all_settings()
                setting_found = False
                
                for category_settings in current_settings.values():
                    for setting in category_settings:
                        if setting['name'] == actual_name:
                            setting_type = setting['type']
                            
                            # Validate numeric ranges
                            if setting_type in ['int', 'float']:
                                try:
                                    numeric_value = float(new_value) if setting_type == 'float' else int(new_value)
                                    
                                    # Check min/max bounds
                                    if setting['min_value'] is not None and numeric_value < setting['min_value']:
                                        flash(f'❌ {actual_name}: Value {numeric_value} is below minimum {setting["min_value"]}', 'error')
                                        continue
                                    if setting['max_value'] is not None and numeric_value > setting['max_value']:
                                        flash(f'❌ {actual_name}: Value {numeric_value} is above maximum {setting["max_value"]}', 'error')
                                        continue
                                        
                                    new_value = numeric_value
                                except ValueError:
                                    flash(f'❌ {actual_name}: Invalid {setting_type} value: {new_value}', 'error')
                                    continue
                            
                            # Update the setting
                            trader.db.set_setting(actual_name, new_value, setting_type)
                            setting_found = True
                            break
                    if setting_found:
                        break
        
        # Reload configuration from database for both global and trader instances
        config.load_from_database()
        if trader:
            trader.config.load_from_database()
        
        flash('✅ Settings updated successfully! Most changes take effect immediately. Connection settings require restart.', 'success')
        
    except Exception as e:
        flash(f'❌ Error updating settings: {str(e)}', 'error')
    
    return redirect(url_for('settings'))

@app.route('/api/status')
def api_status():
    """API endpoint for system status (for AJAX updates)"""
    if not trader:
        init_trader()
    
    active_trades = trader.db.get_active_trades()
    today = datetime.now().strftime('%Y-%m-%d')
    today_count = trader.db.get_trade_count_for_date(today)
    
    # Get current SPX price for API
    spx_price = None
    if trader and trader.is_connected:
        try:
            spx_price = trader.get_spx_price()
        except Exception as e:
            print(f"Error getting SPX price for API: {e}")
    
    return jsonify({
        'connected': trader.is_connected if trader else False,
        'scheduler_running': False,  # Web manager doesn't run scheduler
        'active_positions': len(active_trades),
        'trades_today': today_count,
        'spx_price': spx_price,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/logs')
def view_logs():
    """View system logs"""
    if not trader:
        init_trader()
    
    conn = sqlite3.connect(trader.db.db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT action, message, success, timestamp 
        FROM daily_log 
        ORDER BY timestamp DESC 
        LIMIT 100
    ''')
    raw_logs = cursor.fetchall()
    conn.close()
    
    # Convert timestamps to Eastern Time for display
    logs = []
    eastern_tz = pytz.timezone('US/Eastern')
    
    for action, message, success, timestamp_str in raw_logs:
        try:
            # Parse UTC timestamp
            utc_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            utc_time = pytz.utc.localize(utc_time)
            
            # Convert to Eastern Time
            eastern_time = utc_time.astimezone(eastern_tz)
            formatted_time = eastern_time.strftime('%Y-%m-%d %H:%M:%S %Z')
            
            logs.append((action, message, success, formatted_time))
        except Exception as e:
            # If conversion fails, use original timestamp
            logs.append((action, message, success, timestamp_str))
    
    return render_template('logs.html', logs=logs)

if __name__ == '__main__':
    print("Starting SPX Calendar Trading Web Manager")
    print("Access at: http://localhost:5000")
    print("Use Ctrl+C to stop")
    
    # Initialize read-only database access
    init_trader()
    
    try:
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down web interface...")
        print("System shutdown complete")
