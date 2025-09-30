#!/usr/bin/env python3
"""
SPX Calendar Trading System - Master Launcher
Starts all required components in the correct order
"""

import os
import sys
import time
import threading
import subprocess
from datetime import datetime

def print_banner():
    print("=" * 60)
    print("🚀 SPX CALENDAR TRADING SYSTEM")
    print("=" * 60)
    print(f"📅 Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

def kill_existing_processes():
    """Clean up only SPX-related Python processes"""
    print("🧹 Cleaning up existing SPX Calendar processes...")
    try:
        if sys.platform == "win32":
            # Kill only processes running our specific scripts
            subprocess.run('taskkill /F /FI "WINDOWTITLE eq *spx_double_calendar*"', shell=True, capture_output=True)
            subprocess.run('taskkill /F /FI "WINDOWTITLE eq *spx_web_manager*"', shell=True, capture_output=True)
            # Alternative approach - kill by command line
            subprocess.run('wmic process where "commandline like \'%spx_double_calendar.py%\'" delete', shell=True, capture_output=True)
            subprocess.run('wmic process where "commandline like \'%spx_web_manager.py%\'" delete', shell=True, capture_output=True)
        else:
            # Linux/Mac - kill by process name pattern
            subprocess.run("pkill -f spx_double_calendar.py", shell=True, capture_output=True)
            subprocess.run("pkill -f spx_web_manager.py", shell=True, capture_output=True)
        
        print("✅ SPX Calendar cleanup complete")
        time.sleep(2)  # Give processes time to fully terminate
    except Exception as e:
        print(f"⚠️ Cleanup warning: {e}")

def start_main_trading_system():
    """Start the main trading system (spx_double_calendar.py)"""
    print("🎯 Starting main trading system...")
    print("   - Handles: Trading execution, scheduling, reconciliation")
    print("   - Schedules: 9:44:50 AM trades, 3:00 PM exits, 5:00 PM reconciliation")
    
    try:
        if sys.platform == "win32":
            # Windows - start in new console window
            process = subprocess.Popen([
                "cmd", "/c", "start", "cmd", "/k", 
                f"cd /d {os.getcwd()} && python spx_double_calendar.py"
            ], shell=True)
        else:
            # Linux/Mac - use screen or nohup
            process = subprocess.Popen([
                "python", "spx_double_calendar.py"
            ], preexec_fn=os.setsid)
        
        print("✅ Main trading system started")
        return process
        
    except Exception as e:
        print(f"❌ Failed to start main trading system: {e}")
        return None

def start_web_interface():
    """Start the web dashboard (spx_web_manager.py)"""
    print("🌐 Starting web dashboard...")
    print("   - Access at: http://localhost:5000")
    print("   - Features: Live P&L, manual controls, position management")
    
    try:
        if sys.platform == "win32":
            # Windows - start in new console window
            process = subprocess.Popen([
                "cmd", "/c", "start", "cmd", "/k",
                f"cd /d {os.getcwd()} && python spx_web_manager.py"
            ], shell=True)
        else:
            # Linux/Mac
            process = subprocess.Popen([
                "python", "spx_web_manager.py"
            ], preexec_fn=os.setsid)
        
        print("✅ Web dashboard started")
        return process
        
    except Exception as e:
        print(f"❌ Failed to start web dashboard: {e}")
        return None

def wait_for_services():
    """Wait for services to be ready"""
    print("⏳ Waiting for services to initialize...")
    
    # Wait a bit for processes to start
    time.sleep(5)
    
    # Check if web server is responding
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        result = sock.connect_ex(('localhost', 5000))
        sock.close()
        
        if result == 0:
            print("✅ Web dashboard is ready at http://localhost:5000")
        else:
            print("⚠️ Web dashboard may still be starting...")
    except:
        print("⚠️ Could not verify web dashboard status")

def show_system_status():
    """Show what's running and how to access it"""
    print("\n" + "=" * 60)
    print("🎉 SPX CALENDAR SYSTEM IS RUNNING!")
    print("=" * 60)
    print()
    print("📊 COMPONENTS ACTIVE:")
    print("   ✅ Main Trading System (spx_double_calendar.py)")
    print("      - Automated trading at 9:44:50 AM")
    print("      - Time-based exits at 3:00 PM") 
    print("      - Position reconciliation at 5:00 PM")
    print()
    print("   ✅ Web Dashboard (spx_web_manager.py)")
    print("      - Live P&L monitoring")
    print("      - Manual trade controls")
    print("      - Position management")
    print()
    print("🌐 ACCESS YOUR DASHBOARD:")
    print("   👉 http://localhost:5000")
    print()
    print("🛑 TO STOP THE SYSTEM:")
    print("   - Close both console windows")
    print("   - Or run: python restart_system.py")
    print()
    print("📋 SCHEDULED ACTIVITIES:")
    print("   🕘 9:44:50 AM - Daily trade execution")
    print("   🕒 3:00 PM  - Time-based position exits")
    print("   🕔 5:00 PM  - Position reconciliation check")
    print()
    print("✨ System is ready! Check your dashboard for live updates.")
    print("=" * 60)

def main():
    """Main launcher function"""
    print_banner()
    
    # Step 1: Clean up any existing processes
    kill_existing_processes()
    
    # Step 2: Start main trading system
    main_process = start_main_trading_system()
    if not main_process:
        print("❌ Cannot continue without main trading system")
        return False
    
    # Give main system time to initialize
    time.sleep(3)
    
    # Step 3: Start web interface
    web_process = start_web_interface()
    if not web_process:
        print("⚠️ Web dashboard failed to start, but main system is running")
    
    # Step 4: Wait for services to be ready
    wait_for_services()
    
    # Step 5: Show status and instructions
    show_system_status()
    
    return True

if __name__ == "__main__":
    print("Starting SPX Calendar Trading System...")
    
    try:
        success = main()
        if success:
            print("\n🎯 Launcher complete. System is running independently.")
            print("💡 You can close this window - the system will continue running.")
        else:
            print("\n❌ System startup failed. Check the error messages above.")
    
    except KeyboardInterrupt:
        print("\n\n🛑 Startup cancelled by user")
    except Exception as e:
        print(f"\n❌ Unexpected error during startup: {e}")
    
    input("\nPress Enter to close this window...")
