import os
from dotenv import load_dotenv

load_dotenv()

# Конфигурационный модуль для загрузки переменных окружения в проекте
class GlobalConfig:
    BASE_URL = os.getenv('BASE_URL', 'https://demo.tandoor.dev')
    TOKEN = os.getenv('TANDOOR_TOKEN', '')