import numpy as np
import sys
sys.path.insert(0, '.')

from ets import forecast_ets_seasonality, Aggregation

# ============================================
# Тестовые данные с известной сезонностью
# ============================================
test_cases = {
    # Ваши оригинальные данные
    'A (оригинал)': {
        'values': [33, 14, 22, 21, 42, 42, 36, 25, 26, 12, 27, 36, 
                   32, 36, 19, 38, 30, 47, 19, 45, 36, 44, 11, 29],
        'expected': 2
    },
    'B (оригинал)': {
        'values': [26, 13, 13, 9, 9, 4, 13, 14, 20, 12, 25, 15, 
                   11, 2, 18, 11, 13, 47, 17, 11, 17, 8, 12, 12],
        'expected': 8
    },
    'C (оригинал)': {
        'values': [81, 75, 58, 65, 97, 74, 91, 81, 68, 67, 87, 116, 
                   41, 84, 56, 40, 44, 60, 42, 39, 58, 58, 26, 51],
        'expected': 3
    },
    
    # Идеальные синтетические паттерны (для проверки алгоритма)
    'Идеальный период 2': {
        'values': [10, 20, 10, 20, 10, 20, 10, 20, 10, 20, 10, 20],
        'expected': 2
    },
    'Идеальный период 3': {
        'values': [10, 20, 30, 10, 20, 30, 10, 20, 30, 10, 20, 30],
        'expected': 3
    },
    'Идеальный период 4': {
        'values': [10, 20, 30, 40, 10, 20, 30, 40, 10, 20, 30, 40],
        'expected': 4
    },
    'Идеальный период 6': {
        'values': [10, 20, 30, 40, 50, 60, 10, 20, 30, 40, 50, 60],
        'expected': 6
    },
    'Идеальный период 8': {
        'values': [10, 20, 30, 40, 50, 60, 70, 80, 10, 20, 30, 40, 50, 60, 70, 80],
        'expected': 8
    },
    'Идеальный период 12': {
        'values': [1,2,3,4,5,6,7,8,9,10,11,12, 1,2,3,4,5,6,7,8,9,10,11,12],
        'expected': 12
    },
    
    # Паттерны с шумом (более реалистичные)
    'Период 2 с шумом': {
        'values': [10, 20, 12, 19, 11, 21, 9, 20, 11, 19, 10, 21],
        'expected': 2
    },
    'Период 4 с шумом': {
        'values': [10, 20, 30, 40, 12, 19, 31, 39, 11, 21, 29, 41],
        'expected': 4
    },
    'Период 8 с шумом': {
        'values': [10, 20, 30, 40, 50, 60, 70, 80, 12, 19, 31, 39, 51, 59, 71, 79],
        'expected': 8
    },
}

# ============================================
# Функция отладки автокорреляции
# ============================================
def debug_autocorrelation(values, name):
    """Показывает автокорреляцию для ключевых периодов"""
    n = len(values)
    detrended = values - np.mean(values)
    std = np.std(detrended)
    
    if std < 1e-10:
        return
    
    detrended = detrended / std
    
    print(f"\n{name}:")
    print(f"  Длина данных: {n}")
    print(f"  Автокорреляция по периодам:")
    
    for lag in range(2, min(13, n//2 + 1)):
        if n - lag > 0:
            c = np.corrcoef(detrended[:-lag], detrended[lag:])[0, 1]
            if not np.isnan(c):
                divisor = "✓" if n % lag == 0 else " "
                marker = " <-- MAX" if lag == np.argmax([np.corrcoef(detrended[:-l], detrended[l:])[0, 1] if not np.isnan(np.corrcoef(detrended[:-l], detrended[l:])[0, 1]) else 0 for l in range(2, min(13, n//2 + 1))]) else ""
                print(f"    Период {lag:2d}: {c:+.3f} {divisor}{marker}")

# ============================================
# Запуск тестов
# ============================================
print("=" * 80)
print("Testing FORECAST.ETS.SEASONALITY - Extended Test Suite")
print("=" * 80)

passed = 0
failed = 0

for name, test in test_cases.items():
    values = np.array(test['values'])
    expected = test['expected']
    
    result = forecast_ets_seasonality(
        values=values.tolist(),
        timeline=list(range(len(values))),
        fill_missing=True,
        aggregation=Aggregation.AVERAGE
    )
    
    status = "✓ PASS" if result == expected else "✗ FAIL"
    if result == expected:
        passed += 1
    else:
        failed += 1
        # Отладка для неудачных тестов
        debug_autocorrelation(values, name)
    
    print(f"{status} | {name:25s} | Detected={result:2d} | Expected={expected}")

print("=" * 80)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
print("=" * 80)

# ============================================
# Детальная отладка Product B
# ============================================
print("\n" + "=" * 80)
print("DETAILED DEBUG: Product B")
print("=" * 80)

b_values = np.array([26, 13, 13, 9, 9, 4, 13, 14, 20, 12, 25, 15, 
                     11, 2, 18, 11, 13, 47, 17, 11, 17, 8, 12, 12])

print(f"\nДанные Product B ({len(b_values)} точек):")
print(b_values)

# Проверка паттерна периода 8
print("\nПроверка паттерна периода 8:")
for cycle in range(3):
    start = cycle * 8
    end = start + 8
    if end <= len(b_values):
        print(f"  Цикл {cycle + 1}: {b_values[start:end]}")

# Автокорреляция для периодов 6-10
print("\nАвтокорреляция для периодов 6-10:")
detrended = b_values - np.mean(b_values)
std = np.std(detrended)
if std > 0:
    detrended = detrended / std
    
    for lag in range(6, 11):
        c = np.corrcoef(detrended[:-lag], detrended[lag:])[0, 1]
        if not np.isnan(c):
            divisor = " (делит 24)" if 24 % lag == 0 else ""
            print(f"  Период {lag}: {c:+.4f}{divisor}")

print("=" * 80)
