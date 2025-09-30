#!/usr/bin/env python3
"""
Simple startup script for SPX Double Calendar Spread Trading System
"""

import os
import sys

def main():
    print("🚀 SPX Double Calendar Spread Trading System Launcher")
    print("=" * 55)
    
    # Check if main script exists
    if not os.path.exists('spx_double_calendar.py'):
        print("❌ spx_double_calendar.py not found!")
        print("Make sure you're in the correct directory.")
        return
    
    # Show menu
    print("\nSelect operation mode:")
    print("1. Automatic Mode (runs daily scheduler)")
    print("2. Manual Override Mode (interactive menu)")
    print("3. Test Mode (single execution)")
    print("4. Show setup instructions")
    print("0. Exit")
    
    while True:
        choice = input("\nEnter your choice (0-4): ").strip()
        
        if choice == "0":
            print("👋 Goodbye!")
            break
        elif choice == "1":
            print("🚀 Starting automatic mode...")
            os.system("python spx_double_calendar.py --mode auto")
            break
        elif choice == "2":
            print("🎛️ Starting manual override mode...")
            os.system("python spx_double_calendar.py --mode manual")
            break
        elif choice == "3":
            print("🧪 Starting test mode...")
            os.system("python spx_double_calendar.py --mode test")
            break
        elif choice == "4":
            print("📖 Showing setup instructions...")
            os.system("python spx_calendar_config.py")
            continue
        else:
            print("❌ Invalid choice. Please enter 0-4.")
            continue

if __name__ == "__main__":
    main()
