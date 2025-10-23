#!/usr/bin/env python3
"""
SPX Calendar System Restart Script
Helps restart the system cleanly after connection issues
"""

import os
import sys
import time
import subprocess
import signal
import psutil

def kill_existing_processes():
    """Kill any existing SPX calendar processes"""
    print("🔍 Looking for existing SPX calendar processes...")
    
    killed_count = 0
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
            
            # Look for Python processes running our scripts
            if (proc.info['name'] in ['python.exe', 'python3.exe', 'python'] and 
                any(script in cmdline for script in ['spx_double_calendar.py', 'spx_web_manager.py', 'start_spx_calendar.py'])):
                
                print(f"🔪 Killing process: {proc.info['pid']} - {cmdline[:80]}...")
                proc.terminate()
                killed_count += 1
                
                # Wait a bit, then force kill if needed
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                    print(f"   Force killed {proc.info['pid']}")
                    
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    
    if killed_count > 0:
        print(f"✅ Killed {killed_count} existing processes")
        time.sleep(3)  # Give time for cleanup
    else:
        print("ℹ️ No existing processes found")

def restart_unified_system():
    """Restart the unified trading system (includes web interface)"""
    print("\n🚀 Starting Unified SPX Calendar System...")
    
    try:
        # Change to the script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        os.chdir(script_dir)
        
        # Start the unified system (trading + web interface)
        if os.path.exists('spx_double_calendar.py'):
            subprocess.Popen([sys.executable, 'spx_double_calendar.py'], 
                           creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0)
            print("✅ Unified system started in new window")
            print("   - Trading engine + Web interface")
            print("   - Single process, single IBKR connection")
            print("🌐 Access at: http://localhost:5000")
        else:
            print("❌ spx_double_calendar.py not found")
            return False
            
    except Exception as e:
        print(f"❌ Error starting unified system: {e}")
        return False
    
    return True

def main():
    """Main restart function"""
    print("=" * 50)
    print("🔄 SPX Calendar System Restart")
    print("=" * 50)
    
    try:
        # Step 1: Kill existing processes
        kill_existing_processes()
        
        # Step 2: Wait for IBKR to release connections
        print("\n⏳ Waiting 10 seconds for IBKR to release connections...")
        time.sleep(10)
        
        # Step 3: Restart unified system
        success = restart_unified_system()
        
        if success:
            print("\n✅ System restart completed!")
            print("📋 Next steps:")
            print("   1. Check web interface at http://localhost:5000")
            print("   2. Verify IBKR connection status")
            print("   3. Check streaming is working (Live Price updates)")
            print("   4. Check system logs for any issues")
        else:
            print("\n❌ System restart failed!")
            
    except KeyboardInterrupt:
        print("\n🛑 Restart cancelled by user")
    except Exception as e:
        print(f"\n❌ Unexpected error during restart: {e}")

if __name__ == "__main__":
    main()
