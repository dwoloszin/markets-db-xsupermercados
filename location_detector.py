import requests
from typing import Optional, Tuple

class LocationDetector:
    """Detect user's location and get ZIP code for Brazil."""
    
    @staticmethod
    def get_ip_location() -> Optional[dict]:
        """Get approximate location from user's IP address."""
        try:
            # Using ip-api.com (free, no API key required)
            response = requests.get('http://ip-api.com/json/', timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    return {
                        'latitude': data.get('lat'),
                        'longitude': data.get('lon'),
                        'city': data.get('city'),
                        'region': data.get('regionName'),
                        'country': data.get('country'),
                        'country_code': data.get('countryCode')
                    }
        except Exception as e:
            print(f"⚠️  Could not get IP location: {e}")
        return None

    @staticmethod
    def get_zip_from_coordinates(latitude: float, longitude: float) -> Optional[str]:
        """Get ZIP code from coordinates using ViaCEP reverse geocoding."""
        try:
            # ViaCEP is a Brazilian service that provides address from coordinates
            # Format: https://viacep.com.br/ws/{lat},{lon}/json/
            url = f"https://nominatim.openstreetmap.org/reverse"
            params = {
                'format': 'json',
                'lat': latitude,
                'lon': longitude,
                'zoom': 18,
                'addressdetails': 1
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                address = data.get('address', {})
                
                # Try to get postcode from OSM data
                postcode = address.get('postcode')
                if postcode:
                    return postcode
        except Exception as e:
            print(f"⚠️  Could not get ZIP from coordinates: {e}")
        
        # Fallback: try ViaCEP directly
        return LocationDetector._get_zip_viacep(latitude, longitude)

    @staticmethod
    def _get_zip_viacep(latitude: float, longitude: float) -> Optional[str]:
        """ViaCEP does not support reverse geocoding by coordinates — always returns None."""
        # ViaCEP only accepts a CEP (ZIP code) as input, not lat/lon.
        # Nominatim (called before this fallback) is the correct tool for reverse geocoding.
        return None

    @staticmethod
    def detect_user_location() -> Optional[str]:
        """Detect user's location and return ZIP code."""
        print("📍 Detecting your location...")
        
        # Step 1: Get IP location
        ip_location = LocationDetector.get_ip_location()
        
        if not ip_location:
            print("✗ Could not determine your location from IP")
            return None
        
        print(f"✓ Location detected: {ip_location['city']}, {ip_location['region']}, {ip_location['country']}")
        
        # Check if in Brazil
        if ip_location['country_code'] != 'BR':
            print(f"⚠️  You are in {ip_location['country']}, system is configured for Brazil")
            return None
        
        # Step 2: Get ZIP code from coordinates
        latitude = ip_location['latitude']
        longitude = ip_location['longitude']
        
        print(f"📌 Coordinates: {latitude}, {longitude}")
        print("🔍 Searching for ZIP code...")
        
        zip_code = LocationDetector.get_zip_from_coordinates(latitude, longitude)
        
        if zip_code:
            print(f"✓ ZIP Code found: {zip_code}")
            return zip_code
        else:
            print("✗ Could not determine ZIP code from location")
            # Return a default or None
            return None

    @staticmethod
    def format_zip_code(zip_code: str) -> str:
        """Format ZIP code to XXXXX-XXX format if needed."""
        zip_code = zip_code.replace('-', '').strip()
        
        # Brazilian ZIP codes are 8 digits
        if len(zip_code) == 8:
            return f"{zip_code[:5]}-{zip_code[5:]}"
        
        return zip_code

if __name__ == "__main__":
    # Test location detection
    zip_code = LocationDetector.detect_user_location()
    if zip_code:
        formatted = LocationDetector.format_zip_code(zip_code)
        print(f"\n✓ Final ZIP Code: {formatted}")
    else:
        print("\n✗ Could not detect location. Using default ZIP code: 07110-000")
