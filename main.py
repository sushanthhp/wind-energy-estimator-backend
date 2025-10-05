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

# --- MODIFIED/CORRECTED ENDPOINT ---
# This version is more robust because it extracts the place name and uses
# a geocoding API instead of relying on fragile regex to find coordinates
# in the URL, which often fails for shared place URLs.
@app.post("/resolve-gmaps-url")
@app.post("/resolve-gmaps-url")
def resolve_gmaps_url(req: UrlRequest):
    """
    Resolves a Google Maps URL by extracting the place name, cleaning it,
    and then using a geocoding API to find its coordinates.
    """
    try:
        # Step 1: Follow the redirect to get the final URL
        with requests.Session() as s:
            s.headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = s.get(req.url, allow_redirects=True, timeout=10)
            final_url = response.url
        
        print(f"--- DEBUG: Final resolved URL: {final_url}")

        # Step 2: Extract the place name from the URL.
        match = re.search(r"/place/([^/]+)", final_url)
        if not match:
            # Fallback for URLs that might contain coordinates directly
            coord_match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", final_url)
            if coord_match:
                lat, lon = coord_match.groups()
                return {"latitude": float(lat), "longitude": float(lon)}

            raise HTTPException(
                status_code=400,
                detail="Could not extract a recognizable place name or coordinates from the URL."
            )
        
        # --- NEW IMPROVEMENT: Sanitize the place name ---
        raw_place_name = unquote(match.group(1).replace('+', ' '))
        place_name = raw_place_name.split('|')[0].strip()
        
        print(f"--- DEBUG: Raw place name: {raw_place_name}")
        print(f"--- DEBUG: Cleaned place name for API: {place_name}")

        # Step 3: Use the Open-Meteo Geocoding API with the CLEANED name
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={place_name}&count=1&format=json"
        geo_response = requests.get(geo_url, timeout=10)
        geo_response.raise_for_status()
        geo_data = geo_response.json()

        # Step 4: Check if the API returned any results
        if "results" in geo_data and len(geo_data["results"]) > 0:
            location = geo_data["results"][0]
            location_name_for_display = location.get("name", raw_place_name)
            return {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "found_name": location_name_for_display
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Geocoding service could not find coordinates for '{place_name}'."
            )

    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Failed to resolve URL or contact geocoding service: {e}")@app.get("/search-location")
def search_location(name: str):
    """Proxies a search request to the Open-Meteo geocoding API."""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={name}&count=5&format=json"
    try:
        response = requests.get(url)
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
    for loc in req.locations[:2]:
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
