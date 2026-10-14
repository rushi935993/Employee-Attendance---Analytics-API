# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+
- Git

## Setup on Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"

uvicorn app.main:app --port 8000
```

The application creates its indexes at startup. Environment variables take precedence over values loaded from `.env`.

To load the supplied sample data in a development database, run `python sample_seed.py` before starting the server. The seed script resets the sample collections, so do not run it against data you need to preserve.

## API documentation

- Health check: `http://127.0.0.1:8000/health`
- Interactive API documentation: `http://127.0.0.1:8000/docs`

## Implementation

Attendance operations use MongoDB for persistence and concurrency control. Analytics are implemented using MongoDB aggregation pipelines. `REVIEW.md` documents reviewed starter-code defects and their fixes. `DECISIONS.md` explains the key implementation choices.

The supplied sample data is small. Correctness must also be evaluated against edge cases and larger datasets.