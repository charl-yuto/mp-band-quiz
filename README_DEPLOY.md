# Deploy to Render

1. Create a GitHub repository.
2. Put the contents of this directory at the repository root.
3. Push to GitHub.
4. In Render, create a new Web Service from that GitHub repository.
5. Use Docker / Dockerfile if Render asks.
6. Add environment variable:

```text
MP_API_KEY = your_materials_project_api_key
```

7. Deploy.

If Render asks for Build Command and Start Command while using Dockerfile, leave them empty.
If you deploy without Docker, use:

```bash
Build Command:
cd frontend && npm install && npm run build && cd ../backend && pip install -r requirements.txt

Start Command:
cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT
```
