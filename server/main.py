import os
from dotenv import load_dotenv

load_dotenv()                          

API_KEY = os.getenv("BUS_API_KEY")  
UPSTREAM_URL = os.getenv("BUS_API_URL")  


