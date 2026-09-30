# TNT Driveaway Mileage App

A simple private mileage-entry app for TNT Driveaway.

## What it does

- Each driver receives a private username/password.
- Drivers can submit:
  - Trip date
  - Unit/truck number
  - Pickup city
  - Delivery city
  - Beginning miles
  - Ending miles
  - Notes
- Total miles are calculated automatically.
- Drivers can ONLY view their own entries.
- Admin can view every driver and every mileage entry.
- Admin can create new driver logins.

## Default Admin Login

Username: admin
Password: TNTadmin123!

IMPORTANT: Change this password setup before putting the app on the public internet.

## Run locally

1. Install Python 3.
2. Open a terminal in this folder.
3. Run:

   pip install -r requirements.txt

4. Start the app:

   python app.py

5. Open:

   http://127.0.0.1:5000

## Hosting

This Flask app can be hosted on services such as Render, Railway, PythonAnywhere, or another web host.

For a production version, recommended upgrades include:
- PostgreSQL instead of SQLite
- Password reset/change screen
- HTTPS
- Admin ability to edit/deactivate drivers
- Date filters and weekly totals
- Export to Excel/CSV
- Trip/load number
- Photo uploads for odometer/dash
- Signature or driver acknowledgment
- Mobile home-screen/PWA install
