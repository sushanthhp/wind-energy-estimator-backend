from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import requests
import math
import re

# --- App Setup ---
app = FastAPI()

# Configure CORS to allow requests from your front-end
# For production, you might want to restrict this to your actual front-end's domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# --- Pydantic Models for Request Data Validation ---
class WindRequest(BaseModel):
    latitude: float
    longitude: float
    blade_radius: float
    turbine_height: float

class CompareRequest(BaseModel):
    locations: List[WindRequest]

class UrlRequest(BaseModel):
    url: str

# --- Helper Functions for Calculation ---
def fetch_weather(lat: float, lon: float):
    """Fetches current weather data from Open-Meteo with a 10-second timeout."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        current_weather = data['current_weather']
        return {
            "temperature_C": current_weather['temperature'],
            "wind_speed_mps": current_weather['windspeed'],
            "pressure_hPa": 1013.25 
        }
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=408, detail="Request to weather service timed out.")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Weather service is currently unavailable.")

def calculate_air_density(temp_C: float, pressure_hPa: float) -> float:
    """Calculates air density using the ideal gas law."""
    temp_K = temp_C + 273.15
    pressure_Pa = pressure_hPa * 100
    R_SPECIFIC = 287.05
    return pressure_Pa / (R_SPECIFIC * temp_K)

def adjust_wind_speed_for_height(v_ref: float, h: float, ref_height: float = 10, alpha: float = 0.14) -> float:
    """Adjusts wind speed based on turbine height using the wind power law."""
    return v_ref * (h / ref_height) ** alpha

def calculate_power_output(air_density: float, wind_speed: float, blade_radius: float) -> float:
    """Calculates the theoretical power output of a wind turbine."""
    swept_area = math.pi * blade_radius**2
    return 0.5 * air_density * swept_area * wind_speed**3

# --- API Endpoints ---
@app.post("/resolve-gmaps-url")
def resolve_gmaps_url(req: UrlRequest):
    """Resolves a short Google Maps URL to find and return its coordinates."""
    try:
        # Use a session with headers to better mimic a real browser
        with requests.Session() as s:
            s.headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = s.get(req.url, allow_redirects=True, timeout=5)
            final_url = response.url
        
        # --- DEBUGGING STEP: Print the final URL to the console ---
        print("--- DEBUG ---")
        print(f"Final resolved URL: {final_url}")
        print("---------------")

        # Pattern 1: For URLs with '@lat,lon' (e.g., from a dropped pin)
        match1 = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", final_url)
        if match1:
            lat, lon = match1.groups()
            return {"latitude": float(lat), "longitude": float(lon)}

        # Pattern 2: For URLs with '!3dlat!4dlon' (e.g., for named places)
        match2 = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", final_url)
        if match2:
            lat, lon = match2.groups()
            return {"latitude": float(lat), "longitude": float(lon)}
        
        # If neither pattern matches, raise an error
        raise HTTPException(status_code=400, detail="Could not find coordinates in the final URL.")
    except requests.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Failed to resolve URL: {e}")

@app.get("/search-location")
def search_location(name: str):
    """Proxies a search request to the Open-Meteo geocoding API."""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={name}&count=5&format=json"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        return {"error": str(e)}, 500
    
@app.post("/estimate")
def estimate_power(req: WindRequest):
    """Calculates the estimated power output for a single wind turbine location."""
    weather = fetch_weather(req.latitude, req.longitude)
    air_density = calculate_air_density(weather['temperature_C'], weather['pressure_hPa'])
    adjusted_speed = adjust_wind_speed_for_height(weather['wind_speed_mps'], req.turbine_height)
    power = calculate_power_output(air_density, adjusted_speed, req.blade_radius)
    return {
        "weather": weather,
        "air_density_kg_m3": round(air_density, 3),
        "adjusted_wind_speed_mps": round(adjusted_speed, 2),
        "estimated_power_W": round(power, 2)
    }

@app.post("/compare")
def compare_power(req: CompareRequest):
    """Calculates and compares the power output for two locations."""
    results = []
    for loc in req.locations[:2]:
        weather = fetch_weather(loc.latitude, loc.longitude)
        air_density = calculate_air_density(weather['temperature_C'], weather['pressure_hPa'])
        adjusted_speed = adjust_wind_speed_for_height(weather['wind_speed_mps'], loc.turbine_height)
        power = calculate_power_output(air_density, adjusted_speed, loc.blade_radius)
        results.append({
            "weather": weather,
            "air_density_kg_m3": round(air_density, 3),
            "adjusted_wind_speed_mps": round(adjusted_speed, 2),
            "estimated_power_W": round(power, 2)
        })
    return results

