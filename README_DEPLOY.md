# AlphaBook Deployment Guide

This guide details how to deploy the AlphaBook application to Google Cloud Platform using Cloud Run (backend) and Firebase Hosting (frontend assets + proxy).

## Prerequisites

Ensure you have the following installed and logged in:
-   [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud`)
-   [Firebase CLI](https://firebase.google.com/docs/cli) (`firebase`)
-   A Google Cloud Project with Billing enabled.

## 1. Backend Deployment (Cloud Run)

The FastAPI backend will be containerized and run on Cloud Run.

1.  **Deploy to Cloud Run**:
    Run the following command from the root of the `alphaBook` directory:
    ```bash
    gcloud run deploy alphabook-api \
      --source . \
      --region us-central1 \
      --allow-unauthenticated
    ```
    -   If prompted to enable APIs (e.g., Cloud Build, Cloud Run), say **yes**.
    -   This command builds the Docker container in the cloud and deploys it.

2.  **Set Environment Variables**:
    You need to provide the `service-account.json` credentials to the running container.
    
    *Option A: Secrets Manager (Recommended)*
    1.  Upload `service-account.json` to Google Secret Manager.
    2.  Expose it as a volume or env var.
    
    *Option B: Base64 Env Var (Simpler for testing)*
    1.  Encode your `service-account.json` to base64:
        ```bash
        base64 -i service-account.json
        # Copy the output
        ```
    2.  Go to the Cloud Run console -> `alphabook-api` -> Verify/Edit -> Variables.
    3.  Add `FIREBASE_CREDENTIALS_JSON` and paste the base64 string (or raw JSON if it fits).
    
    *App Code Note*: The app currently looks for `GOOGLE_APPLICATION_CREDENTIALS` pointing to a file. You might need to update `app/db.py` or `main.py` detailed below to handle the env var content if you don't use the file path. **Wait, the current SETUP.md mentions `FIREBASE_CREDENTIALS_JSON` support in section 6.** Let's verify that code exists.

## 2. Frontend Deployment (Firebase Hosting)

Firebase Hosting will serve the static files and rewrite `/api/*` (and other routes) to Cloud Run.

1.  **Initialize Firebase** (if not done):
    ```bash
    firebase init hosting
    ```
    -   Project: Select your existing project.
    -   Public directory: `app/static` (or just `static`? Check your structure. It is `app/static`).
    -   Configure as single-page app: **No** (FastAPI handles routing).
    -   Overwrite index.html: **No**.

2.  **Verify `firebase.json`**:
    Ensure it looks like this (I have created this file for you):
    ```json
    {
      "hosting": {
        "public": "app/static",
        "rewrites": [
          {
            "source": "**",
            "run": {
              "serviceId": "alphabook-api",
              "region": "us-central1"
            }
          }
        ]
      }
    }
    ```

3.  **Deploy**:
    ```bash
    firebase deploy --only hosting
    ```

## 3. Post-Deployment Verification

1.  Visit the Hosting URL (e.g., `https://your-project.web.app`).
2.  It should load the AlphaBook home page.
3.  Check logs in Cloud Console if there are issues.
