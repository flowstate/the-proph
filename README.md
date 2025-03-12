# Prophet Service

A companion service that provides time-series forecasting for Tractor-Beam using Facebook's Prophet library.

## Features

- Demand forecasting with external regressors (MTI, inflation)
- Supplier performance prediction (quality ratings and lead time reliability)
- Visualization of forecasts with confidence intervals

## Installation

1. Clone the repository
2. Set up a Python virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

Start the service:

```bash
python app.py
```

The service will try to run on port 5001, with fallbacks to 5002 and 5000 if needed.

## API Endpoints

- `POST /predict/demand` - Generate demand forecasts
- `POST /predict/supplier-performance` - Predict supplier performance metrics

See the API documentation for request/response formats and examples.