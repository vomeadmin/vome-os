web: uvicorn main:app --host 0.0.0.0 --port $PORT
worker: celery -A vomeos.worker worker -Q support,default --concurrency 4 --loglevel info
beat: celery -A vomeos.worker beat --loglevel info
