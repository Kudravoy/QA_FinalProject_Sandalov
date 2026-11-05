import pytest
import json
from api.client import TandoorAPIClient
import os
from dotenv import load_dotenv
load_dotenv()
import time
from selenium import webdriver
import pickle
from pathlib import Path
from datetime import datetime, timedelta
import shutil
from typing import Literal

import allure
from selenium.webdriver.chrome.options import Options

"""Работоспособность браузера, API клиента, создание скриншотов при неудачном тестировании"""

# Запускает и закрывает браузер при тестировании
@pytest.fixture
def driver():
    options = Options()

    options.add_argument("--start-maximized")

    is_ci = os.getenv("CI", "false").lower() == "true"
    headless = os.getenv("HEADLESS", "false").lower() == "true"

    if is_ci or headless:
        print("🔧 Запуск в headless-режиме")
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-popup-blocking")
    else:
        print("🖥️ Запуск с отображением окна")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = None
    try:
        # НЕ указываем путь к chromedriver — Selenium сам найдёт/скачает
        driver = webdriver.Chrome(options=options)
        driver.implicitly_wait(10)
        yield driver
    except Exception as e:
        print(f"❌ Ошибка создания драйвера: {e}")
        raise
    finally:
        if driver:
            driver.quit()

# Cоздает класс TandoorAPIClient в фикстурах
@pytest.fixture(scope="session")
def api_client():
    """
    Фикстура, возвращающая экземпляр API клиента.
    Данные для тестов берутся из переменных окружения.
    """
    # Получаем данные из .env
    token = os.getenv('TANDOOR_TOKEN')
    # Проверяем наличие токена
    assert token, "TANDOOR_TOKEN не был обнаружен!"
    return TandoorAPIClient()

#Автоматически создает скриншот при неудачном тестировании
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    if report.when == "call" and report.failed:
        driver = item.funcargs.get("driver")
        if driver:
            # Путь расположения файлов
            screenshot_dir = Path(__file__).parent.parent / "screenshots"
            screenshot_dir.mkdir(exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = screenshot_dir / f"{item.name}_{timestamp}.png"
            driver.save_screenshot(str(screenshot_path))
            print(f"\n📸 Скриншот сохранён: {screenshot_path}")

#Управляет скриншотами: удаляет при успешном прохождении и сохраняет при падении тестов
@pytest.fixture(scope="session", autouse=True)
def manage_screenshots():
    """Управляет скриншотами: удаляет при успехе, сохраняет при падении"""
    screenshot_dir = Path(__file__).parent.parent / "screenshots"

    # Очищает старые скриншоты
    if screenshot_dir.exists():
        shutil.rmtree(screenshot_dir)

    # Создаёт папку заново
    screenshot_dir.mkdir(exist_ok=True)

    yield  # тесты выполняются

    # Если тесты прошли успешно — папка остаётся пустой, если были падения — скриншоты уже сохранены
    # Проверка:
    screenshots = list(screenshot_dir.glob("*.png"))
    if not screenshots and screenshot_dir.exists():
        # Нет скриншотов — удаляем пустую папку
        shutil.rmtree(screenshot_dir)

#Автоматическая авторизация пользователя на сайте с использованием cookies
@pytest.fixture
def auth_driver(driver):
    cookie_file = "cookies.pkl"
    driver.get("https://tandoor.vs1.srv.eduson.tv")

    # Загружаем cookies
    try:
        with open(cookie_file, "rb") as f:
            cookies = pickle.load(f)
        driver.delete_all_cookies()
        for cookie in cookies:
            driver.add_cookie(cookie)
        driver.refresh()
        time.sleep(2)
    except FileNotFoundError:
        from pages.login_page import LoginPage
        LoginPage(driver).login()
        time.sleep(3)
        with open(cookie_file, "wb") as f:
            pickle.dump(driver.get_cookies(), f)

    # --- УНИВЕРСАЛЬНЫЙ ПЕРЕХВАТЧИК CSRF ---
    csrf_token = driver.get_cookie("csrftoken")
    if csrf_token:
        token = csrf_token['value']
        driver.execute_script(f"""
            // Перехватываем fetch
            let originalFetch = window.fetch;
            window.fetch = function(url, options = {{}}) {{
                options.headers = options.headers || {{}};
                options.headers['X-CSRFToken'] = '{token}';
                return originalFetch.call(this, url, options);
            }};
            // Перехватываем XMLHttpRequest
            let originalOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url) {{
                this.setRequestHeader('X-CSRFToken', '{token}');
                return originalOpen.apply(this, arguments);
            }};
        """)

    return driver

"""Взаимодействие с рецептами"""

# Использует существующий рецепт или создает новый (по наименованию)
@pytest.fixture
def recipe_provider(api_client, recipe_preparer):
    """
    Универсальная фикстура для получения рецепта.
    Использование: recipe_provider('name') или recipe_provider('id')
    """

    def _get_recipe(return_field: Literal['name', 'id']):
        # Пытается использовать существующий рецепт
        recipes = api_client.get_recipes()
        if recipes and recipes.get('results'):
            first_recipe = recipes['results'][0]
            result = first_recipe.get(return_field)
            print(f"✅ Используем существующий рецепт: {first_recipe.get('name')} (ID: {first_recipe.get('id')})")
            return result

        # В случае отсутствия — импортирует новый рецепт
        url = "https://eda.rambler.ru/recepty/pasta-picca/domashnyaya-picca-140418"
        import_result = api_client.import_recipe_from_source(url)

        if not import_result or import_result.get('error'):
            pytest.skip(f"Не удалось распарсить рецепт: {import_result}")

        # Сохраняет импортированный рецепт
        save_data = recipe_preparer(import_result, url)
        save_result = api_client.create_recipe(save_data)

        if not save_result or 'id' not in save_result:
            pytest.skip(f"Не удалось сохранить рецепт: {save_result}")

        result = save_result.get(return_field)
        print(f"✅ Создан новый рецепт: {save_result.get('name')} (ID: {save_result.get('id')})")
        return result

    return _get_recipe

# Подготавливает данные рецепта для сохранения через импортирование рецепта
def prepare_recipe_data(import_result, url):

    recipe = import_result.get('recipe', {})
    steps = []

    for idx, step in enumerate(recipe.get('steps', [])):
        step_dict = {
            "name": f"Шаг {idx + 1}",
            "instruction": step if isinstance(step, str) else step.get('instruction', ''),
            "order": idx,
            "ingredients": []
        }

        # Добавляем ингредиенты если есть
        if isinstance(step, dict) and step.get('ingredients'):
            for ing_idx, ing in enumerate(step['ingredients']):
                if isinstance(ing, str):
                    ing = {"name": ing}
                ing['order'] = ing_idx
                step_dict['ingredients'].append(ing)

        steps.append(step_dict)

    return {
        "name": recipe.get('name', ''),
        "description": recipe.get('description', ''),
        "servings": recipe.get('servings', 1),
        "steps": steps,
        "source_url": recipe.get('source_url', url)
    }

# Использует подготовленные данные рецепта при сохранении
@pytest.fixture
def recipe_preparer():
    return prepare_recipe_data

@pytest.fixture
def cleanup_recipes(api_client):
    """Фикстура для удаления рецептов после теста"""
    created_recipe_ids = []

    def add_recipe_id(recipe_id):
        created_recipe_ids.append(recipe_id)

    yield add_recipe_id

    # После теста удаляем все созданные рецепты
    for recipe_id in created_recipe_ids:
        try:
            api_client.delete_recipe(recipe_id)
            print(f"🧹 Рецепт {recipe_id} удален")
        except Exception as e:
            print(f"❌ Ошибка при удалении рецепта {recipe_id}: {e}")

"""Взаимодействие c планом питания"""

# Создает план питания и удаляет его после прохождения теста
@pytest.fixture
def create_and_delete_meal_plan(api_client):
    created_plans = []

    # Создает план питания
    def _create_plan(plan_data):
        plan = api_client.create_meal_plan(plan_data)
        if plan and 'id' in plan:
            created_plans.append(plan['id'])
            print(f"📝 Создан план с ID: {plan['id']}")
        return plan

    yield _create_plan

    # Удаляет созданный план питания после теста
    print(f"\n🧹 Очистка: удаляем {len(created_plans)} план(ов)...")
    for plan_id in created_plans:
        try:
            api_client.delete_meal_plan(plan_id)
            print(f"✓ Удален план с ID: {plan_id}")
        except Exception as e:
            print(f"✗ Ошибка при удалении плана {plan_id}: {e}")


# Автоматически удаляет все планы питания после теста
@pytest.fixture
def delete_meal_plans_after_test(api_client):
    """Фикстура для автоматического удаления всех планов питания после теста"""
    # Сохраняем ID планов, которые были до теста
    response_before = api_client.get_meal_plan_list()
    plans_before_ids = {plan['id'] for plan in response_before.get('results', [])}

    yield  # Здесь выполняется тест

    # После теста удаляем все новые планы (которых не было до теста)
    response_after = api_client.get_meal_plan_list()
    all_plans = response_after.get('results', [])

    deleted_count = 0
    for plan in all_plans:
        if plan['id'] not in plans_before_ids:
            api_client.delete_meal_plan(plan['id'])
            deleted_count += 1
            print(f"✅ Удалён созданный план: {plan['id']}")

    if deleted_count > 0:
        print(f"✅ Всего удалено {deleted_count} новых планов")
    else:
        print("ℹ️ Новых планов для удаления не найдено")

"""Взаимодействие с данными"""

# Загружает данных из файла "recipe_links.json"
def load_recipe_links():
    """Загружает список ссылок на рецепты из папки test_data"""
    # Получаем путь к корню проекта (поднимаемся на 2 уровня от tests/conftest.py)
    conftest_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"📍 conftest.py находится в: {conftest_dir}")

    # Если conftest.py в папке tests, то корень проекта - на уровень выше
    if conftest_dir.endswith('tests'):
        project_root = os.path.dirname(conftest_dir)
    else:
        # Если conftest.py в корне проекта
        project_root = conftest_dir

    json_path = os.path.join(project_root, 'test_data', 'recipe_links.json')

    print(f"🔍 Ищем файл: {json_path}")

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            links = data.get("recipe_links", [])
            print(f"✅ Загружено {len(links)} ссылок")
            return links
    except FileNotFoundError:
        print(f"❌ Файл не найден: {json_path}")
        print("💡 Убедитесь, что файл существует: test_data/recipe_links.json")
        return []
    except json.JSONDecodeError as e:
        print(f"❌ Ошибка парсинга JSON: {e}")
        return []

# Использует список ссылок на рецепты
@pytest.fixture
def recipe_links():
    """Фикстура со списком ссылок на рецепты"""
    return load_recipe_links()

#Данные, необходимые при создании рецепта в ручную через API
@pytest.fixture
def recipe_data():
    return {
        "name": "Курица в тандыре целиком",
        "steps": [
            {
                "name": "Подготовка курицы",
                "instruction": "Промойте курицу, удалите внутренности. Сделайте несколько надрезов  для проникновения маринада",
                "order": 0,
                "ingredients": []
            }
        ],
    }

@pytest.fixture
def recipe_data_to_enter():
    return {
        "name": "Пипе Ригате Barilla",
        "description": "Вкусное блюдо из пасты с картофелем и беконом",
        "key_word": "Паста - Пипе Ригате Barilla",
        "waiting_time": 25,
        "working_time": 10,
        "portion": 5,
        "portion_description": "На большую семью",
        "instruction": f"""Очистите картофель от кожуры и нарежьте его небольшими кубиками.
            Залейте картофель водой и оставьте на несколько минут.
            В небольшой сковороде на среднем огне обжарьте картофель в сливочном масле в течение 10–15 минут, помешивая не чаще одного раза в 5 минут. Приправьте солью и перцем.
            Пока обжаривается картофель, отварите Пипе Ригате в большом количестве подсоленной воды 10 минут до состояния аль денте.
            В отдельной сковороде обжарьте нарезанный бекон.
            Добавьте бекон к картофелю и готовьте еще 2–3 минуты.
            Перемешайте пасту с картофелем и беконом.
            Готовое блюдо сбрызните оливковым маслом и украсьте листочками базилика"""
        }

#Данные, необходимые при создании плана питания
@pytest.fixture
def data_for_meal_plan():
    today = datetime.now().strftime("%Y-%m-%d")

    return {
        "recipe": "Рагу с тушенкой",
        "heading": "Тестовое блюдо",
        "day_number": today,
        "hour": 13,
        "minute": 50,
        "type_food": "Завтрак",
        "number_servings": 18,
        "note": "план наполнен успешно"
    }

#Данные, необходимые при создании плана питания через API по наименованию
@pytest.fixture
def data_for_meal_plan_by_API(recipe_provider):
    """Фикстура для создания плана питания через API по имени рецепта"""

    # Получаем имя рецепта через новую фикстуру
    recipe_name = recipe_provider('name')

    tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

    return {
        "recipe": {"name": recipe_name},  # Используем имя рецепта
        "from_date": f"{tomorrow}T00:00:00",
        "to_date": f"{tomorrow}T00:00:00",
        "meal_type": {"name": "Завтрак"},
        "servings": 2
    }

#Данные, необходимые при создании плана питания через API через ID
@pytest.fixture
def data_for_meal_plan_by_API_id(recipe_provider):
    """Фикстура для создания плана питания через API по ID"""

    # Получаем ID рецепта через новую фикстуру
    recipe_id = recipe_provider('id')

    # Если ID не получен, пробуем получить снова (резервный вариант)
    if recipe_id is None:
        # Пробуем получить рецепт напрямую
        from api.client import TandoorAPIClient
        api = TandoorAPIClient()
        recipes = api.get_recipes()
        if recipes and recipes.get('results'):
            recipe_id = recipes['results'][0]['id']
            print(f"✅ Получен ID рецепта напрямую: {recipe_id}")

    # Если все равно None, пропускаем тест
    if recipe_id is None:
        pytest.skip("Не удалось получить ID рецепта для создания плана")

    tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

    return {
        "recipe": recipe_id,  # Используем полученный ID
        "from_date": f"{tomorrow}T00:00:00",
        "to_date": f"{tomorrow}T00:00:00",
        "meal_type": {"name": "Завтрак"},
        "servings": 2
    }
# def data_for_meal_plan_by_API_id(get_or_create_recipe_id):
#     """Фикстура для создания плана питания через API по ID"""
#
#     # Если ID не получен, пробуем получить снова
#     if get_or_create_recipe_id is None:
#         # Пробуем получить рецепт напрямую
#         from api.client import TandoorAPIClient
#         api = TandoorAPIClient()
#         recipes = api.get_recipes()
#         if recipes and recipes.get('results'):
#             get_or_create_recipe_id = recipes['results'][0]['id']
#             print(f"✅ Получен ID рецепта напрямую: {get_or_create_recipe_id}")
#
#     # Если все равно None, пропускаем тест
#     if get_or_create_recipe_id is None:
#         pytest.skip("Не удалось получить ID рецепта для создания плана")
#
#     tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
#
#     return {
#         "recipe": get_or_create_recipe_id,
#         "from_date": f"{tomorrow}T00:00:00",
#         "to_date": f"{tomorrow}T00:00:00",
#         "meal_type": {"name": "Завтрак"},
#         "servings": 2
#     }

#Данные, необходимые при создании типов питания
@pytest.fixture
def meal_types():
    return [
        {"name": "Завтрак"},
        {"name": "Обед"},
        {"name": "Ужин"}
    ]

#Данные, необходимые для добавления позиций в лист покупок (произвольно)
@pytest.fixture
def shopping_list_data():
    return {
        'food': {'name': 'Мясо'},
        'amount': 1000,
        'unit': {'name': 'гр'}
    }

#Данные, необходимые для добавления позиций в лист покупок (по рецепту)
@pytest.fixture
def shopping_list_data_on_recipe(api_client):

    # Получаем первый рецепт
    response = api_client.get_recipes()

    if response and 'results' in response and len(response['results']) > 0:
        first_recipe_id = response['results'][0]['id']
    else:
        pytest.skip("Нет доступных рецептов")

    return {
        'name': 'Тестовый список покупок',
        'recipe': first_recipe_id,  # ← Добавляем recipe в фикстуру
        'servings': 15
    }