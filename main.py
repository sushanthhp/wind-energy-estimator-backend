from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import requests
import math
import re
from urllib.parse import unquote

# --- App Setup ---
app = FastAPI()

# Configure CORS to allow requests from your front-end
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins, restrict in production
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
    """Fetches current weather data from Open-Meteo using coordinates."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        current_weather = response.json()['current_weather']
        return {
            "temperature_C": current_weather['temperature'],
            "wind_speed_mps": current_weather['windspeed'],
            "pressure_hPa": 1013.25  # Standard pressure approximation
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
    """
    Resolves a Google Maps URL using the powerful Nominatim (OpenStreetMap) API,
    which has a much more comprehensive database of places.
    """
    try:
        with requests.Session() as s:
            s.headers = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36' }
            response = s.get(req.url, allow_redirects=True, timeout=10)
            final_url = response.url

        coord_match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", final_url)
        if coord_match:
            lat, lon = coord_match.groups()
            return {"latitude": float(lat), "longitude": float(lon), "isAreaResult": False}

        match = re.search(r"/place/([^/]+)", final_url)
        if not match:
            raise HTTPException(status_code=400, detail="Could not find a place name in the URL.")

        raw_place_name = unquote(match.group(1).replace('+', ' '))
        search_name = raw_place_name.split('|')[0].strip()
        
        geo_url = f"https://nominatim.openstreetmap.org/search?q={search_name}&format=json&limit=1"
        headers = {'User-Agent': 'WindEnergyEstimator/1.0 (Contact: your-email@example.com)'}
        
        geo_response = requests.get(geo_url, headers=headers, timeout=10)
        geo_response.raise_for_status()
        geo_data = geo_response.json()

        if geo_data:
            location = geo_data[0]
            return {
                "latitude": float(location["lat"]),
                "longitude": float(location["lon"]),
                "found_name": location.get("display_name"),
                "isAreaResult": False
            }
        else:
            raise HTTPException(status_code=404, detail=f"Nominatim could not find a location for '{search_name}'.")

    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Service connection error: {e}")

# --- THIS ENDPOINT IS NOW UPGRADED TO USE NOMINATIM ---
@app.get("/search-location")
def search_location(name: str):
    """
    Proxies a search request to the Nominatim (OpenStreetMap) API,
    which provides much better and more detailed results for the search dropdown.
    """
    url = f"https://nominatim.openstreetmap.org/search?q={name}&format=json&limit=5"
    
    # Per Nominatim's policy, we MUST provide a custom User-Agent
    headers = {
        'User-Agent': 'WindEnergyEstimator/1.0 (Contact: your-email@example.com)'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Could not connect to the geocoding service: {e}")

@app.post("/estimate")
def estimate_power(req: WindRequest):
    """Calculates the estimated power output for a single wind turbine location."""
    weather = fetch_weather(req.latitude, req.longitude)
    air_density = calculate_air_density(weather['temperature_C'], weather['pressure_hPa'])
    adjusted_speed = adjust_wind_speed_for_height(weather['wind_speed_mps'], req.turbine_height)
    power = calculate_power_output(air_density, adjusted_speed, req.blade_radius)
    return {
        "location_input": {"latitude": req.latitude, "longitude": req.longitude},
        "weather": weather,
        "air_density_kg_m3": round(air_density, 3),
        "adjusted_wind_speed_mps": round(adjusted_speed, 2),
        "estimated_power_W": round(power, 2)
    }

@app.post("/compare")
def compare_power(req: CompareRequest):
    """Calculates and compares the power output for two locations."""
    results = []
    for loc in req.locations[:2]: # Process a maximum of two locations
        weather = fetch_weather(loc.latitude, loc.longitude)
        air_density = calculate_air_density(weather['temperature_C'], weather['pressure_hPa'])
        adjusted_speed = adjust_wind_speed_for_height(weather['wind_speed_mps'], loc.turbine_height)
        power = calculate_power_output(air_density, adjusted_speed, loc.blade_radius)
        results.append({
            "location_input": {"latitude": loc.latitude, "longitude": loc.longitude},
            "weather": weather,
            "air_density_kg_m3": round(air_density, 3),
            "adjusted_wind_speed_mps": round(adjusted_speed, 2),
            "estimated_power_W": round(power, 2)
        })
    return results
