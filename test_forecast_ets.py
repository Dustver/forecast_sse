# test_seasonality.py
from ets import forecast_ets_seasonality, DataCompletion, Aggregation

test_data = {
    'A': [33, 14, 22, 21, 42, 42, 36, 25, 26, 12, 27, 36, 32, 36, 19, 38, 30, 47, 19, 45, 36, 44, 11, 29],
    'B': [26, 13, 13, 9, 9, 4, 13, 14, 20, 12, 25, 15, 11, 2, 18, 11, 13, 47, 17, 11, 17, 8, 12, 12],
    'C': [81, 75, 58, 65, 97, 74, 91, 81, 68, 67, 87, 116, 41, 84, 56, 40, 44, 60, 42, 39, 58, 58, 26, 51]
}

expected = {'A': 2, 'B': 8, 'C': 3}

print("Testing seasonality detection:")
for sku, values in test_data.items():
    result = forecast_ets_seasonality(values=values, timeline=list(range(len(values))))
    status = "✓" if result == expected[sku] else "✗"
    print(f"{status} {sku}: Detected={result}, Expected={expected[sku]}")