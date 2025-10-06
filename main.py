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

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models ---
class WindRequest(BaseModel):
    latitude: float
    longitude: float
    blade_radius: float
    turbine_height: float

class CompareRequest(BaseModel):
    locations: List[WindRequest]

class UrlRequest(BaseModel):
    url: str

# --- Helper Functions ---
def fetch_weather(lat: float, lon: float):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        current_weather = response.json()['current_weather']
        return {
            "temperature_C": current_weather['temperature'],
            "wind_speed_mps": current_weather['windspeed'],
            "pressure_hPa": 1013.25
        }
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Weather service is currently unavailable.")

def calculate_air_density(temp_C: float, pressure_hPa: float) -> float:
    temp_K = temp_C + 273.15
    pressure_Pa = pressure_hPa * 100
    R_SPECIFIC = 287.05
    return pressure_Pa / (R_SPECIFIC * temp_K)

def adjust_wind_speed_for_height(v_ref: float, h: float, ref_height: float = 10, alpha: float = 0.14) -> float:
    return v_ref * (h / ref_height) ** alpha

def calculate_power_output(air_density: float, wind_speed: float, blade_radius: float) -> float:
    """
    Calculates the estimated electrical power output of a wind turbine.
    This version includes key efficiency factors for a more realistic estimate.
    """
    swept_area = math.pi * blade_radius**2
    power_in_wind = 0.5 * air_density * swept_area * wind_speed**3

    # --- Efficiency Factors ---
    # Power coefficient (Cp): Accounts for aerodynamic efficiency (Betz's Law). Cannot exceed ~0.59.
    power_coefficient_Cp = 0.45
    # Gearbox efficiency: Accounts for mechanical losses, including gear resistance.
    gearbox_efficiency = 0.97
    # Generator efficiency: Accounts for losses in converting mechanical to electrical energy.
    generator_efficiency = 0.96

    # Calculate the final electrical power by applying all efficiency losses
    electrical_power_output = power_in_wind * power_coefficient_Cp * gearbox_efficiency * generator_efficiency
    
    return electrical_power_output

# --- API Endpoints ---

@app.post("/resolve-gmaps-url")
def resolve_gmaps_url(req: UrlRequest):
    try:
        with requests.Session() as s:
            # Use a full, valid User-Agent to ensure Google redirects correctly
            s.headers = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36' }
            response = s.get(req.url, allow_redirects=True, timeout=10)
            final_url = response.url

        coord_match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", final_url)
        if coord_match:
            lat, lon = coord_match.groups()
            return {"latitude": float(lat), "longitude": float(lon), "isAreaResult": False}

        match = re.search(r"/place/([^/]+)", final_url)
        if not match:
            raise HTTPException(status_code=400, detail="Could not find a place name in the URL after redirection.")

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

@app.get("/search-location")
def search_location(name: str):
    url = f"https://nominatim.openstreetmap.org/search?q={name}&format=json&limit=5"
    headers = {'User-Agent': 'WindEnergyEstimator/1.0 (Contact: your-email@example.com)'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Could not connect to the geocoding service: {e}")

@app.post("/estimate")
def estimate_power(req: WindRequest):
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
