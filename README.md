# Scraper Flask App

This repository contains a Flask web application that scrapes carrier email addresses from the FMCSA website and generates a CSV file based on MC numbers using the SaferWeb API.

## Features
- Web interface to input MC number range
- Scrapes carrier emails using Selenium and ChromeDriver
- Generates downloadable CSV files

## Requirements
- Python 3.7+
- Google Chrome or Chromium
- ChromeDriver (matching your Chrome version)
- pip (Python package manager)

## Setup on AWS Linux

### 1. Install System Dependencies
```bash
sudo yum update -y
sudo yum install -y python3 python3-pip unzip wget

# Install Google Chrome
wget https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
sudo yum install -y ./google-chrome-stable_current_x86_64.rpm

# Find your Chrome version
google-chrome --version

# Download matching ChromeDriver (replace VERSION as needed)
CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+')
wget https://chromedriver.storage.googleapis.com/$(curl -sS https://chromedriver.storage.googleapis.com/LATEST_RELEASE_$CHROME_VERSION)/chromedriver_linux64.zip
unzip chromedriver_linux64.zip
sudo mv chromedriver /usr/bin/
sudo chmod +x /usr/bin/chromedriver
```

### 2. Clone the Repository
```bash
git clone <your-repo-url>.git
cd Scraper
```

### 3. Install Python Dependencies
```bash
pip3 install -r requirements.txt
```

### 4. Set Environment Variables
Before running the app, set your SAFER API key:
```bash
export SAFER_API_KEY=your_api_key_here
```
If you installed ChromeDriver to a custom path, set the `CHROMEDRIVER_PATH` environment variable:
```bash
export CHROMEDRIVER_PATH=/path/to/chromedriver
```

### 5. Run the App
```bash
python3 app.py
```

The app will be available at `http://localhost:5000/`.

## Usage
- Open the web interface.
- Enter the MC number range.
- Download the generated CSV file.

## Notes
- Make sure the `templates/index.html` file exists for the web interface.
- For production, consider using a WSGI server (e.g., Gunicorn) and a reverse proxy (e.g., Nginx). 