# AlphaBook Setup Guide

## Prerequisites

- **Python 3.9+** installed.
- **Google Account** for Firebase.
- Use `virtualenv` or `venv` to isolate dependencies.

## 1. Firebase Setup

You need both "Admin" credentials (for the backend) and "Web" configuration (for the frontend).

### Step 1: Create Project
1.  Go to the [Firebase Console](https://console.firebase.google.com/).
2.  Click **Add project** and follow the prompts.

### Step 2: Enable Services
1.  **Authentication**: 
    - Go to Build -> Authentication.
    - Click **Get Started**.
    - Enable **Email/Password** and **Google** providers.
2.  **Firestore Database**:
    - Go to Build -> Firestore Database.
    - Click **Create Database**.
    - Start in **Production mode**.
    - Choose a location close to you.
3.  **Storage**:
    - Go to Build -> Storage.
    - Click **Get Started**.
    - Start in **Production mode**.

### Step 3: Get Backend Service Account Key (The "Missing Info")
This is the file that lets your Python server talk to Firebase.

1.  Click the **Gear icon** (Settings) in the top-left sidebar -> **Project settings**.
2.  Go to the **Service accounts** tab (it's the 4th tab).
3.  Scroll down. You will see a "Python" snippet snippet option? Ignore that. Just click **Generate new private key** (blue button).
4.  Confirm by clicking **Generate key**.
5.  A file will download. **This is your key.**
6.  **Action**: Move this file into your `alphaBook` folder.
7.  **Action**: Rename it to `service-account.json`.

### Step 4: Get Frontend Web Config
This is what lets your login page talk to Firebase.

1.  Stay in **Project settings**.
2.  Go to the **General** tab (1st tab).
3.  Scroll down to the bottom section called "Your apps".
4.  If you haven't created an app yet, click the **</>** (Web) circle icon.
    - Give it a name like "AlphaBook".
    - Click "Register app".
5.  You will see a code block titled "Add Firebase SDK". Look for `const firebaseConfig = { ... };`.
6.  **Action**: You need to copy the values inside the `firebaseConfig` object (apiKey, authDomain, etc.) and paste them into your `.env` file.

## 2. Configuration

1.  **Create .env file**:
    ```bash
    cp .env.example .env
    ```

2.  **Edit .env**:
    Fill in the details using the files/config you just generated.

    ```ini
    # Application Secret (for session signing)
    SECRET_KEY="change_this_to_a_random_string"

    # Backend Auth (Service Account)
    # Path to the JSON file from Step 3
    GOOGLE_APPLICATION_CREDENTIALS="service-account.json"
    FIREBASE_PROJECT_ID="your-project-id"
    # Found in Storage tab or Project Settings
    FIREBASE_STORAGE_BUCKET="your-project-id.appspot.com"

    # Frontend Auth (Project Settings -> General -> Your Apps)
    # These values come from the 'firebaseConfig' object in Step 4
    FIREBASE_API_KEY="AIzaSy..."
    FIREBASE_AUTH_DOMAIN="your-project.firebaseapp.com"
    FIREBASE_PROJECT_ID_WEB="your-project-id"
    FIREBASE_STORAGE_BUCKET_WEB="your-project.appspot.com"
    FIREBASE_MESSAGING_SENDER_ID="1234..."
    FIREBASE_APP_ID="1:1234..."
    ```

## 3. Installation

Install Python dependencies:

```bash
pip install -r requirements.txt
```

## 4. Running the Application

Start the development server:

```bash
python -m uvicorn app.main:app --reload
```

## 5. Verification

1.  Open [http://localhost:8000](http://localhost:8000).
2.  **Sign Up**: Create a new account.
3.  **Check Firestore**: Go to Firebase Console -> Firestore Database. You should see a new document in the `users` collection.
4.  **Trading**: Place an order on the trading page.
    - Verify `orders` and `trades` collections are created in Firestore.
5.  **File Upload**:
    - Build a simple form or use generic API tool to POST to `/files/upload` with a file and Authorization header (session cookie is handled by browser, but for external test you need auth).
    - Easier: Just check that "Storage bucket initialized" appears in the server logs on validation startup.
