# Telegram Bot Project

## Overview

This is a simple Telegram bot application written in Python that responds to the `/start` command with a Khmer language message. The bot uses the Telegram Bot API directly via HTTP requests to avoid library conflicts and ensures reliable operation. The bot runs as @Coupon2025_Robot.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

The application follows a simple HTTP-based architecture:

- **Main Bot Logic**: `bot.py` contains the core bot functionality using direct API calls
- **Configuration Management**: Configuration is embedded in the main file for simplicity
- **Logging Strategy**: File-based and console logging with UTF-8 support for Khmer text
- **API Integration**: Uses `requests` library for direct HTTP communication with Telegram Bot API
- **Concurrency Model**: Uses a bounded worker pool for Telegram updates and a separate background pool for non-critical database/message cleanup tasks.

The architecture avoids complex library dependencies and ensures reliable operation through direct API usage.

## Key Components

### Bot Functions
- **handle_message()**: Main message processor with state management
- **send_message()**: Direct API message sending
- **get_updates()**: Polling for new messages
- **send_start_banner()**: Sends the welcome banner and reuses Telegram's cached file ID after the first upload for faster `/start` responses.
- **Thread-local Reply Context**: Keeps reply targets isolated per worker so concurrent users do not affect each other's replies.
- **Channel Post Relay**: Copies posts from the configured `TELEGRAM_CHANNEL_ID` channel to the admin private chat.

### User Commands
- **/start**: Available to all users, sends Khmer account selection message with inline keyboard
- **គូប៉ុង E-GetS Button**: Persistent keyboard button that shows account selection interface

### Stock Management Features
- **Smart Stock Display**: Shows available stock count for each account type
- **Out-of-Stock Indicators**: Displays "សូមអភ័យទោស អស់ពីស្តុក 🪤" for empty account types
- **Visual Distinction**: Clear differentiation between purchasable and out-of-stock items
- **Automatic Stock Updates**: Real-time stock count updates as accounts are sold
- **Fast Callback Handling**: Inline button clicks are acknowledged immediately to reduce Telegram loading delays during buying and payment steps.

### Purchase Flow (Non-Admin Users)
1. **Account Selection**: Click inline buttons to select account type (e.g., "ទិញ Facebook - មានក្នុងស្តុក 5")
   - Inline buttons use short hashed callback IDs so long account type names do not exceed Telegram callback limits.
2. **Quantity Input**: Enter desired quantity after seeing stock and price information
3. **Purchase Confirmation**: Review order details with inline buttons:
   - 🚫 បោះបង់ (Cancel Purchase)
   - ✅ បញ្ជាក់ការទិញ (Confirm Purchase)
4. **KHQR Payment Generation**: Upon confirmation, bot generates Bakong KHQR with:
   - Unique transaction ID
   - Total amount in USD
   - Merchant details (E-GetS Top Up)  
   - QR code string for mobile payment
5. **Payment Verification**: System maintains session with MD5 hash for payment tracking

### Admin Commands (ID: 5002402843)
- **/add_account**: Starts account addition workflow
  - Step 1: Input accounts in format "phone | password"
  - Step 2: Input account type
  - Step 3: Set price per account
  - Completion: Confirms addition with count, type, and price

### Session Management
- **user_sessions**: Tracks conversation state for multi-step workflows
- **accounts_data**: Stores account information, types, and pricing
- `/start` clears stale user purchase sessions before showing the account selection menu.
- Non-critical session saves run in the background to keep customer-facing responses fast.

### Logging System
- **Dual Output**: Logs to both console (stdout) and file (`bot.log`)
- **Unicode Support**: Proper UTF-8 encoding for Khmer text handling
- **Structured Logging**: Consistent format with timestamps and log levels

## Data Flow

1. **Bot Initialization**: Application starts with token from environment/config
2. **Command Processing**: User sends `/start` command to bot
3. **Message Response**: Bot replies with predefined Khmer message
4. **Logging**: All interactions and errors are logged with user information
5. **Error Handling**: Graceful error management with user-friendly messages

## External Dependencies

### Core Dependencies
- **requests**: HTTP library for API communication
- **Standard Library**: `logging`, `time`, `json`, `sys` for core functionality

### Bot Configuration
- `BOT_TOKEN`: Telegram bot authentication token (stored securely as `TELEGRAM_BOT_TOKEN` secret)
- `BOT_USERNAME`: @Coupon2025_Robot
- `KHMER_MESSAGE`: "ជ្រើសរើស Account ដើម្បីបញ្ជាទិញ"
- `TELEGRAM_CHANNEL_ID`: Optional channel/group ID for successful purchase notifications. When set, successful payment notifications are sent to both the admin and this channel. Channels usually use an ID like `-1001234567890` and the bot must be an admin/member there.

### Third-party Services
- **Telegram Bot API**: Direct HTTP API integration for message handling and user interaction

## Recent Changes

- Added optional successful purchase notifications to a configured Telegram channel via `TELEGRAM_CHANNEL_ID`.
- Added channel post relay so messages posted in the configured channel are copied to the admin private chat. E-GetS verification messages are reformatted before sending to the admin as title, email, and code, then automatically deleted after 1 minute. The bot also looks up the buyer by email in purchase history, sends the same formatted verification code to that buyer, and deletes the buyer copy after 1 minute.

## Deployment Strategy

The application is designed for simple deployment with minimal configuration:

### Environment Setup
- Python 3.x runtime required
- Bot token configuration through environment variables
- Unicode/UTF-8 support for Khmer text rendering

### Scalability Considerations
- Single-threaded design suitable for moderate traffic
- Stateless architecture allows for easy horizontal scaling
- File-based logging may need rotation for production use

### Current Limitations
- Incomplete error handler implementation
- Basic command structure (only `/start` implemented)
- No database integration for user data persistence
- No webhook support (polling-based operation assumed)

The architecture provides a solid foundation for a Telegram bot with proper separation of concerns and room for future enhancements like additional commands, user state management, and database integration.