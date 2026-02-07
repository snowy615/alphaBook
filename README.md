
# AlphaBook!
This project uses VS Code Dev Containers and Docker for a consistent development environment.

Prerequisites

Make sure you have the following installed:

Docker

Visual Studio Code

VS Code Dev Containers extension

Getting Started
1. Open the Project in a Dev Container

Clone this repository.

Open the project folder in VS Code.

Install the Dev Containers extension if prompted.

VS Code should automatically detect the .devcontainer configuration.

When prompted, click “Reopen in Container”.

VS Code will build and start the dev container automatically.

2. Set Up the Python Virtual Environment

Inside the dev container terminal:

python -m venv venv


Activate the virtual environment:

Linux / macOS

source venv/bin/activate


Windows

venv\Scripts\activate

3. Install Dependencies

With the virtual environment activated, install the required packages:

pip install -r requirements.txt

4. Run the Application

Start the app using uvicorn:

uvicorn main:app --reload

Once running, the app should be accessible at:

http://localhost:8000


#How the Application works
Admin
Username: Admin
Password: alphabook