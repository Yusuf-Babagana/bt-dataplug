# BT DataPlug - Premium VTU Platform

BT DataPlug is a professional, secure, and mobile-responsive Virtual Top-Up (VTU) application built with Django. It provides a seamless experience for purchasing Data bundles, Airtime, and Cable TV subscriptions with automated wallet funding and instant delivery.

## 🚀 Key Features

- **Automated Funding**: Integrated with **Monnify** to provide users with unique virtual bank accounts for instant wallet crediting.
- **Secure Transactions**: Mandatory 4-digit **Transaction PIN** system protect user funds from unauthorized access.
- **Instant Delivery**: Powered by **ClubKonnect API** for real-time automated delivery of data, airtime, and cable subscriptions.
- **Professional Receipts**: High-quality, branded receipts with "Print" and "Share to WhatsApp" capabilities.
- **Live Dashboard**: Real-time wallet balance refresh and transaction history tracking.
- **WhatsApp Support**: Built-in floating support button for instant customer service.
- **Premium UI**: Modern, clean, and mobile-first design using Bootstrap 5 and Glassmorphism aesthetics.

## 🛠 Tech Stack

- **Backend**: Python / Django
- **Frontend**: Bootstrap 5, FontAwesome, AOS (Animate On Scroll)
- **Database**: SQLite (Development) / PostgreSQL (Production)
- **APIs**: Monnify (Payments/KYC), ClubKonnect (VTU Services)

## 📦 Installation & Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Yusuf-Babagana/bt-dataplug.git
   cd bt-dataplug
   ```

2. **Set up virtual environment**:
   ```bash
   python -v venv venv
   .\venv\Scripts\activate  # Windows
   source venv/bin/activate  # Linux/Mac
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables**:
   Create a `.env` file or set the following in your environment:
   - `MONNIFY_API_KEY`
   - `MONNIFY_SECRET_KEY`
   - `MONNIFY_CONTRACT_CODE`
   - `MY_PERSONAL_BVN` (Proxy KYC)
   - `MY_PERSONAL_NIN` (Proxy KYC)
   - `CLUBKONNECT_USERID`
   - `CLUBKONNECT_APIKEY`

5. **Run Migrations**:
   ```bash
   python manage.py makemigrations
   python manage.py migrate
   ```

6. **Start the server**:
   ```bash
   python manage.py runserver
   ```

## 🔐 Security Features

- **Mandatory PIN**: All users must set a custom 4-digit security PIN before accessing core platform features.
- **Atomic Transactions**: Wallet deductions and API calls are wrapped in atomic blocks to prevent fund loss during network failures.
- **Automatic Refunds**: System automatically restores user balance if a purchase fails at the provider level.

## 📝 License

Distributed under the MIT License. See `LICENSE` for more information.

---
**BT DataPlug** - *Reliability at your fingertips.*
