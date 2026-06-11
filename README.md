python venv .venv
cd backend
python -m pip install -r requirements.txt
uvicorn main:app --reload --port 8000
open live server locally => http://127.0.0.1:5500/index.html