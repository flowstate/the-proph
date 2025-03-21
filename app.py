import base64
import io
import logging
import traceback

import matplotlib

matplotlib.use("Agg")  # Use the 'Agg' backend which doesn't require a GUI
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS
from prophet import Prophet
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def validate_demand_input(data):
    """Validate input data structure and return any issues"""
    required_fields = {"historicalData": list, "futurePeriods": int}

    issues = []

    for field, expected_type in required_fields.items():
        if field not in data:
            issues.append(f"Missing required field: {field}")
        elif not isinstance(data[field], expected_type):
            issues.append(
                f"Invalid type for {field}: expected {expected_type.__name__}, got {type(data[field]).__name__}"
            )

    if not issues and len(data["historicalData"]) == 0:
        issues.append("historicalData array is empty")

    if not issues:
        sample_point = data["historicalData"][0]
        required_point_fields = ["date", "demand"]
        for field in required_point_fields:
            if field not in sample_point:
                issues.append(
                    f"Missing required field in historicalData points: {field}"
                )

    return issues


def generate_mti_projections(historical_mti, periods=90):
    """Generate MTI projections based on historical patterns"""
    # Convert to DataFrame
    df = pd.DataFrame(
        {"ds": pd.to_datetime(historical_mti["date"]), "y": historical_mti["mti"]}
    )

    # Create a simple Prophet model for MTI
    model = Prophet(yearly_seasonality=True)
    model.fit(df)

    # Generate future dataframe
    future = model.make_future_dataframe(periods=periods)

    # Make predictions
    forecast = model.predict(future)

    # Extract only the future values
    future_mti = forecast[forecast["ds"] > df["ds"].max()]["yhat"].tolist()

    return future_mti


def predict_regressor(historical_df, regressor_name, periods, location_id=None):
    """Predict future values for a regressor using Prophet

    Args:
        historical_df: DataFrame with 'ds' and regressor column
        regressor_name: Name of the regressor column
        periods: Number of periods to predict
        location_id: Optional location ID for location-specific predictions
    """
    # Filter by location if provided
    if location_id and "locationId" in historical_df.columns:
        df = historical_df[historical_df["locationId"] == location_id].copy()
    else:
        df = historical_df.copy()

    # Prepare data for Prophet
    prophet_df = pd.DataFrame({"ds": df["ds"], "y": df[regressor_name]})

    # Create and fit model
    model = Prophet(yearly_seasonality=True)
    model.fit(prophet_df)

    # Generate future dataframe
    future = model.make_future_dataframe(periods=periods)
    forecast = model.predict(future)

    # Return only the future predictions
    future_values = forecast[forecast["ds"] > prophet_df["ds"].max()]["yhat"].tolist()
    return future_values


@app.route("/predict/demand", methods=["POST"])
def predict_demand():
    try:
        data = request.json
        logger.info(
            f"Received demand prediction request for {len(data.get('historicalData', []))} data points"
        )

        # Validate input
        validation_issues = validate_demand_input(data)
        if validation_issues:
            error_response = {
                "error": "Invalid input data",
                "details": validation_issues,
            }
            logger.error(f"Returning 400 error: {error_response}")
            return jsonify(error_response), 400

        # Extract historical data
        historical_data = data["historicalData"]
        location_id = data["locationId"]  # Now required
        model_id = data["modelId"]  # Now required

        try:
            # Convert to DataFrame
            df = pd.DataFrame(historical_data)

            # Log data shape and date range
            logger.info(f"Data shape: {df.shape}")

            # Convert date strings to datetime
            df["ds"] = pd.to_datetime(df["date"]).dt.tz_localize(
                None
            )  # Strip timezone, treat as UTC
            df["y"] = df["demand"]

            logger.info(f"Date range: {df['ds'].min()} to {df['ds'].max()}")

            # Data quality checks
            if df["y"].isnull().any():
                null_dates = df[df["y"].isnull()]["ds"].tolist()
                error_response = {
                    "error": "Missing demand values detected",
                    "details": f"Found {len(null_dates)} rows with missing demand values",
                    "example_dates": null_dates[:3],
                }
                logger.error(f"Returning 400 error: {error_response}")
                return jsonify(error_response), 400

            if not pd.to_numeric(df["y"], errors="coerce").notnull().all():
                error_response = {
                    "error": "Non-numeric demand values detected",
                    "details": "All demand values must be numeric",
                }
                logger.error(f"Returning 400 error: {error_response}")
                return jsonify(error_response), 400

            # Check for data density and gaps
            date_range = (df["ds"].max() - df["ds"].min()).days
            expected_points = date_range + 1
            density = len(df) / expected_points

            logger.info(
                f"Data density: {density:.2%} ({len(df)} points over {date_range} days)"
            )

            if density < 0.7:  # Arbitrary threshold, adjust as needed
                error_response = {
                    "error": "Insufficient data density",
                    "details": f"Only {density:.2%} of days have data points. Need at least 70% coverage.",
                }
                logger.error(f"Returning 400 error: {error_response}")
                return jsonify(error_response), 400

            # Find gaps larger than 7 days
            df_sorted = df.sort_values("ds")
            gaps = df_sorted["ds"].diff()
            large_gaps = gaps[gaps > pd.Timedelta(days=7)]
            if not large_gaps.empty:
                logger.warning(f"Found {len(large_gaps)} gaps > 7 days:")
                for idx in large_gaps.index:
                    gap_start = df_sorted.loc[idx - 1, "ds"]
                    gap_end = df_sorted.loc[idx, "ds"]
                    logger.warning(
                        f"  Gap from {gap_start} to {gap_end} ({(gap_end - gap_start).days} days)"
                    )

            # Initialize Prophet model
            model = Prophet(interval_width=0.95)

            # Handle regressors
            future_periods = data.get("futurePeriods", 90)
            regressors_added = []
            future_regressors = {}

            # Handle MTI regressor
            if "mti" in df.columns:
                model.add_regressor("mti")
                regressors_added.append("mti")

                # Check if future MTI values were provided
                if "futureRegressors" in data and "mti" in data["futureRegressors"]:
                    logger.info("Using provided MTI projections")
                    future_regressors["mti"] = data["futureRegressors"]["mti"]
                else:
                    logger.info("Generating MTI projections")
                    # Use all MTI data regardless of location
                    all_mti_data = df[["ds", "mti"]].drop_duplicates()
                    logger.info(f"Using {len(all_mti_data)} unique MTI datapoints")
                    future_regressors["mti"] = predict_regressor(
                        all_mti_data, "mti", future_periods
                    )

            # Handle inflation regressor - always generate for the specific location
            if "inflation" in df.columns:
                model.add_regressor("inflation")
                regressors_added.append("inflation")

                logger.info(f"Generating inflation projections for location {location_id}")
                future_regressors["inflation"] = predict_regressor(
                    df, "inflation", future_periods, location_id
                )

            if regressors_added:
                logger.info(f"Added regressors: {', '.join(regressors_added)}")

            # Fit model
            logger.info("Fitting Prophet model...")
            model.fit(df)
            logger.info("Model fitting completed")

            # Make future dataframe for predictions
            logger.info(f"Creating future dataframe for {future_periods} periods")
            # Get the last date in the historical data
            last_historical_date = df["ds"].max()
            logger.info(f"Last historical date: {last_historical_date}")

            # Create future dataframe starting from the day AFTER the last historical date
            future = model.make_future_dataframe(
                periods=future_periods,
                freq="D",
                include_history=False,  # This is the key change
            )

            # Log the date range to verify
            logger.info(
                f"Future dates range: {future['ds'].min()} to {future['ds'].max()}"
            )

            # Add future regressors to the future dataframe
            for regressor_name, regressor_values in future_regressors.items():
                logger.info(
                    f"Adding {len(regressor_values)} {regressor_name} values to future dataframe"
                )

                # Verify lengths match
                if len(regressor_values) != len(future):
                    logger.error(
                        f"Mismatch in lengths: {regressor_name} has {len(regressor_values)} values but future has {len(future)} rows"
                    )

                # Add the regressor to the future dataframe
                future[regressor_name] = regressor_values

                # Check for NaN values after adding
                nan_count = future[regressor_name].isna().sum()
                if nan_count > 0:
                    logger.error(
                        f"Found {nan_count} NaN values in {regressor_name} after adding to future dataframe"
                    )

            # Make predictions
            logger.info("Generating forecast...")
            forecast = model.predict(future)
            logger.info("Forecast generated successfully")

            # Generate plot
            fig = model.plot(forecast)
            img = io.BytesIO()
            fig.savefig(img, format="png")
            img.seek(0)
            plot_url = base64.b64encode(img.getvalue()).decode()
            plt.close(fig)

            # Format the forecast for JSON response
            forecast_result = []
            for i, row in forecast.iterrows():
                forecast_result.append(
                    {
                        "date": row["ds"].strftime("%Y-%m-%d"),
                        "value": float(row["yhat"]),
                        "lower": float(row["yhat_lower"]),
                        "upper": float(row["yhat_upper"]),
                    }
                )

            # Extract seasonality and trend components from the forecast
            try:
                yearly_std = forecast["yearly"].std()
                yhat_std = forecast["yhat"].std()
                trend_std = forecast["trend"].std()

                # Check for zero or NaN values
                if pd.notnull(yearly_std) and pd.notnull(yhat_std) and yhat_std > 0:
                    seasonality_strength = abs(yearly_std / yhat_std)
                else:
                    seasonality_strength = 0.0

                if pd.notnull(trend_std) and pd.notnull(yhat_std) and yhat_std > 0:
                    trend_strength = abs(trend_std / yhat_std)
                else:
                    trend_strength = 0.0
            except Exception as e:
                logger.warning(
                    f"Error calculating seasonality/trend strength: {str(e)}"
                )
                seasonality_strength = 0.0
                trend_strength = 0.0

            return jsonify(
                {
                    "forecast": forecast_result,
                    "plot": plot_url,
                    "locationId": location_id,
                    "modelId": model_id,
                    "metadata": {
                        "confidenceInterval": 0.95,  # Prophet's default
                        "seasonalityStrength": float(min(seasonality_strength, 1.0)),
                        "trendStrength": float(min(trend_strength, 1.0)),
                    },
                    "futureRegressors": {
                        "mti": future_regressors.get("mti", []),
                        "inflation": future_regressors.get("inflation", [])
                    },
                    "debugInfo": {
                        "dataPoints": len(df),
                        "dateRange": {
                            "start": df["ds"].min().strftime("%Y-%m-%d"),
                            "end": df["ds"].max().strftime("%Y-%m-%d"),
                        },
                        "regressorsUsed": regressors_added,
                        "futurePeriods": future_periods,
                        "generatedRegressors": list(future_regressors.keys()),
                    },
                }
            )

        except pd.errors.EmptyDataError:
            error_response = {
                "error": "Empty dataset provided",
                "details": "The historical data array contains no valid data points",
            }
            logger.error(f"Returning 400 error: {error_response}")
            return jsonify(error_response), 400
        except ValueError as ve:
            error_response = {"error": "Data processing error", "details": str(ve)}
            logger.error(f"Returning 400 error: {error_response}")
            return jsonify(error_response), 400
        except Exception as e:
            logger.error(f"Unexpected error in data processing: {str(e)}")
            logger.error(traceback.format_exc())
            error_response = {
                "error": "Unexpected error during data processing",
                "details": str(e),
                "type": str(type(e).__name__),
            }
            logger.error(f"Returning 500 error: {error_response}")
            return jsonify(error_response), 500

    except Exception as e:
        logger.error(f"Fatal error in predict_demand: {str(e)}")
        logger.error(traceback.format_exc())
        error_response = {
            "error": "Fatal error",
            "details": str(e),
            "type": str(type(e).__name__),
        }
        logger.error(f"Returning 500 error: {error_response}")
        return jsonify(error_response), 500


def validate_supplier_performance_input(data):
    """Validate supplier performance input data structure and return any issues"""
    required_fields = {
        "historicalData": list,
        "futurePeriods": int,
        "supplierId": str
    }

    issues = []

    for field, expected_type in required_fields.items():
        if field not in data:
            issues.append(f"Missing required field: {field}")
        elif not isinstance(data[field], expected_type):
            issues.append(
                f"Invalid type for {field}: expected {expected_type.__name__}, got {type(data[field]).__name__}"
            )

    if not issues and len(data["historicalData"]) == 0:
        issues.append("historicalData array is empty")

    if not issues:
        sample_point = data["historicalData"][0]
        required_point_fields = ["date", "qualityRating", "leadTimeReliability"]
        for field in required_point_fields:
            if field not in sample_point:
                issues.append(
                    f"Missing required field in historicalData points: {field}"
                )

    return issues


@app.route("/predict/supplier-performance", methods=["POST"])
def predict_supplier_performance():
    try:
        data = request.json
        logger.info(
            f"Received supplier performance prediction request for supplier {data.get('supplierId')} with {len(data.get('historicalData', []))} data points"
        )

        # Validate input
        validation_issues = validate_supplier_performance_input(data)
        if validation_issues:
            error_response = {
                "error": "Invalid input data",
                "details": validation_issues,
            }
            logger.error(f"Returning 400 error: {error_response}")
            return jsonify(error_response), 400

        # Extract data
        historical_data = data["historicalData"]
        future_periods = data.get("futurePeriods", 90)
        supplier_id = data["supplierId"]

        try:
            # Convert to DataFrame
            df = pd.DataFrame(historical_data)

            # Log data shape and date range
            logger.info(f"Data shape: {df.shape}")

            # Convert date strings to datetime
            df["ds"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

            # Check for data density and gaps
            date_range = (df["ds"].max() - df["ds"].min()).days
            expected_points = date_range + 1
            density = len(df) / expected_points

            logger.info(
                f"Data density: {density:.2%} ({len(df)} points over {date_range} days)"
            )

            if density < 0.7:  # Arbitrary threshold, adjust as needed
                logger.warning(f"Low data density: {density:.2%}. Results may be less reliable.")

            # Find gaps larger than 7 days
            df_sorted = df.sort_values("ds")
            gaps = df_sorted["ds"].diff()
            large_gaps = gaps[gaps > pd.Timedelta(days=7)]
            if not large_gaps.empty:
                logger.warning(f"Found {len(large_gaps)} gaps > 7 days")

            # Create separate forecasts for quality and lead time
            quality_forecast = []
            lead_time_forecast = []

            # Generate quality forecast
            logger.info("Generating quality rating forecast...")
            quality_df = df[["ds", "qualityRating"]].copy()
            quality_df["y"] = quality_df["qualityRating"]

            # Check for data quality issues
            if quality_df["y"].isnull().any():
                logger.warning(f"Found {quality_df['y'].isnull().sum()} null values in quality data")
                quality_df = quality_df.dropna(subset=["y"])

            if not pd.to_numeric(quality_df["y"], errors="coerce").notnull().all():
                logger.warning("Found non-numeric values in quality data")
                quality_df["y"] = pd.to_numeric(quality_df["y"], errors="coerce")
                quality_df = quality_df.dropna(subset=["y"])

            # Initialize and fit quality model
            quality_model = Prophet(
                interval_width=0.95,
                yearly_seasonality=True,
                weekly_seasonality=True,
                daily_seasonality=False,
                changepoint_prior_scale=0.05  # More flexible for quality changes
            )

            quality_model.fit(quality_df)

            # Make future dataframe for quality predictions
            quality_future = quality_model.make_future_dataframe(
                periods=future_periods,
                freq="D",
                include_history=False
            )

            # Generate quality forecast
            quality_forecast_df = quality_model.predict(quality_future)

            # Generate quality plot
            quality_fig = quality_model.plot(quality_forecast_df)
            quality_img = io.BytesIO()
            quality_fig.savefig(quality_img, format="png")
            quality_img.seek(0)
            quality_plot_url = base64.b64encode(quality_img.getvalue()).decode()
            plt.close(quality_fig)

            # Format quality forecast for JSON response
            for i, row in quality_forecast_df.iterrows():
                quality_forecast.append(
                    {
                        "date": row["ds"].strftime("%Y-%m-%d"),
                        "value": min(1.0, max(0.0, float(row["yhat"]))),  # Clamp between 0 and 1
                        "lower": min(1.0, max(0.0, float(row["yhat_lower"]))),
                        "upper": min(1.0, max(0.0, float(row["yhat_upper"]))),
                    }
                )

            # Generate lead time reliability forecast
            logger.info("Generating lead time reliability forecast...")
            lead_time_df = df[["ds", "leadTimeReliability"]].copy()
            lead_time_df["y"] = lead_time_df["leadTimeReliability"]

            # Check for data quality issues
            if lead_time_df["y"].isnull().any():
                logger.warning(f"Found {lead_time_df['y'].isnull().sum()} null values in lead time data")
                lead_time_df = lead_time_df.dropna(subset=["y"])

            if not pd.to_numeric(lead_time_df["y"], errors="coerce").notnull().all():
                logger.warning("Found non-numeric values in lead time data")
                lead_time_df["y"] = pd.to_numeric(lead_time_df["y"], errors="coerce")
                lead_time_df = lead_time_df.dropna(subset=["y"])

            # Initialize and fit lead time model
            lead_time_model = Prophet(
                interval_width=0.95,
                yearly_seasonality=True,
                weekly_seasonality=True,
                daily_seasonality=False,
                changepoint_prior_scale=0.1  # More flexible for lead time changes
            )

            lead_time_model.fit(lead_time_df)

            # Make future dataframe for lead time predictions
            lead_time_future = lead_time_model.make_future_dataframe(
                periods=future_periods,
                freq="D",
                include_history=False
            )

            # Generate lead time forecast
            lead_time_forecast_df = lead_time_model.predict(lead_time_future)

            # Generate lead time plot
            lead_time_fig = lead_time_model.plot(lead_time_forecast_df)
            lead_time_img = io.BytesIO()
            lead_time_fig.savefig(lead_time_img, format="png")
            lead_time_img.seek(0)
            lead_time_plot_url = base64.b64encode(lead_time_img.getvalue()).decode()
            plt.close(lead_time_fig)

            # Format lead time forecast for JSON response
            for i, row in lead_time_forecast_df.iterrows():
                lead_time_forecast.append(
                    {
                        "date": row["ds"].strftime("%Y-%m-%d"),
                        "value": min(1.0, max(0.0, float(row["yhat"]))),  # Clamp between 0 and 1
                        "lower": min(1.0, max(0.0, float(row["yhat_lower"]))),
                        "upper": min(1.0, max(0.0, float(row["yhat_upper"]))),
                    }
                )

            # Calculate seasonality and trend strengths
            try:
                # For quality
                quality_yearly_std = quality_forecast_df["yearly"].std()
                quality_yhat_std = quality_forecast_df["yhat"].std()
                quality_trend_std = quality_forecast_df["trend"].std()

                # For lead time
                lead_time_yearly_std = lead_time_forecast_df["yearly"].std()
                lead_time_yhat_std = lead_time_forecast_df["yhat"].std()
                lead_time_trend_std = lead_time_forecast_df["trend"].std()

                # Calculate strengths
                if pd.notnull(quality_yearly_std) and pd.notnull(quality_yhat_std) and quality_yhat_std > 0:
                    quality_seasonality_strength = min(1.0, abs(quality_yearly_std / quality_yhat_std))
                else:
                    quality_seasonality_strength = 0.0

                if pd.notnull(quality_trend_std) and pd.notnull(quality_yhat_std) and quality_yhat_std > 0:
                    quality_trend_strength = min(1.0, abs(quality_trend_std / quality_yhat_std))
                else:
                    quality_trend_strength = 0.0

                if pd.notnull(lead_time_yearly_std) and pd.notnull(lead_time_yhat_std) and lead_time_yhat_std > 0:
                    lead_time_seasonality_strength = min(1.0, abs(lead_time_yearly_std / lead_time_yhat_std))
                else:
                    lead_time_seasonality_strength = 0.0

                if pd.notnull(lead_time_trend_std) and pd.notnull(lead_time_yhat_std) and lead_time_yhat_std > 0:
                    lead_time_trend_strength = min(1.0, abs(lead_time_trend_std / lead_time_yhat_std))
                else:
                    lead_time_trend_strength = 0.0

                # Average the strengths
                seasonality_strength = (quality_seasonality_strength + lead_time_seasonality_strength) / 2
                trend_strength = (quality_trend_strength + lead_time_trend_strength) / 2

            except Exception as e:
                logger.warning(f"Error calculating seasonality/trend strength: {str(e)}")
                seasonality_strength = 0.0
                trend_strength = 0.0

            # Prepare the response
            response = {
                "supplierId": supplier_id,
                "qualityForecast": quality_forecast,
                "leadTimeForecast": lead_time_forecast,
                "metadata": {
                    "confidenceInterval": 0.95,
                    "seasonalityStrength": float(seasonality_strength),
                    "trendStrength": float(trend_strength),
                },
                "plots": {
                    "quality": quality_plot_url,
                    "leadTime": lead_time_plot_url
                },
                "debugInfo": {
                    "dataPoints": len(df),
                    "dateRange": {
                        "start": df["ds"].min().strftime("%Y-%m-%d"),
                        "end": df["ds"].max().strftime("%Y-%m-%d"),
                    },
                    "futurePeriods": future_periods,
                }
            }

            logger.info("Successfully generated supplier performance forecast")
            return jsonify(response)

        except pd.errors.EmptyDataError:
            error_response = {
                "error": "Empty dataset provided",
                "details": "The historical data array contains no valid data points",
            }
            logger.error(f"Returning 400 error: {error_response}")
            return jsonify(error_response), 400
        except ValueError as ve:
            error_response = {"error": "Data processing error", "details": str(ve)}
            logger.error(f"Returning 400 error: {error_response}")
            return jsonify(error_response), 400
        except Exception as e:
            logger.error(f"Unexpected error in data processing: {str(e)}")
            logger.error(traceback.format_exc())
            error_response = {
                "error": "Unexpected error during data processing",
                "details": str(e),
                "type": str(type(e).__name__),
            }
            logger.error(f"Returning 500 error: {error_response}")
            return jsonify(error_response), 500

    except Exception as e:
        logger.error(f"Fatal error in predict_supplier_performance: {str(e)}")
        logger.error(traceback.format_exc())
        error_response = {
            "error": "Fatal error",
            "details": str(e),
            "type": str(type(e).__name__),
        }
        logger.error(f"Returning 500 error: {error_response}")
        return jsonify(error_response), 500


if __name__ == "__main__":
    # Try ports in sequence until we find an available one
    ports = [5001, 5002, 5000]  # Prefer 5001, fallback to others

    for port in ports:
        try:
            logger.info(f"Starting Prophet Service on http://localhost:{port}")
            logger.info("Available endpoints:")
            logger.info("  - POST /predict/demand")
            logger.info("  - POST /predict/supplier-performance")
            app.run(debug=True, host="0.0.0.0", port=port)
            break
        except OSError as e:
            if port == ports[-1]:  # Last port attempt
                logger.error(f"Could not start server. All ports {ports} are in use.")
                raise e
            logger.warning(f"Port {port} is in use, trying next port...")
