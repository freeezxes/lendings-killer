# Тестирование UI (Playwright)

Для автоматического и надежного тестирования веб-интерфейса и бизнес-логики агентам рекомендуется использовать **Playwright**.

### Инструкция для AI-агента
1. **Виртуальное окружение**: В проекте (из-за системных ограничений) используется локальное окружение `venv_playwright`.
   Перед работой активируйте его: `source venv_playwright/bin/activate`.
2. **Написание тестов**: Пишите скрипты на Python с использованием синхронного API (`playwright.sync_api`).
3. **Запуск**: Выполняйте скрипты через терминал, например: `source venv_playwright/bin/activate && python test_playwright.py`.
4. **Пример базового скрипта**:
```python
from playwright.sync_api import sync_playwright

def test_example():
    with sync_playwright() as p:
        # headless=True для запуска без графического окна в терминале
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://localhost:8000") # Замените на нужный URL
        
        # Пример взаимодействия:
        # page.fill("input[name='email']", "test@example.com")
        # page.click("text=Отправить")
        
        print(f"Title: {page.title()}")
        browser.close()

if __name__ == "__main__":
    test_example()
```
