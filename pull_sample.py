import requests

# INCOG's live ArcGIS FeatureServer for Tulsa County parcels
BASE_URL = "https://map11.incog.org/arcgis11wa/rest/services/Parcels_TulsaCo/FeatureServer/0/query"

params = {
    "where": "1=1",       # no filter - return all records (within the page size below)
    "outFields": "*",     # return every available field
    "f": "json",          # response format
    "resultOffset": 0,    # starting record - increase this to page through the data
    "resultRecordCount": 10,  # how many records to pull in this request
}

response = requests.get(BASE_URL, params=params)
response.raise_for_status()
data = response.json()

features = data.get("features", [])
print(f"Pulled {len(features)} records\n")

for feature in features:
    attrs = feature["attributes"]
    print(attrs.get("PropertyAddress"), "|", attrs.get("Owner"), "|", attrs.get("TotalAcctValue"))