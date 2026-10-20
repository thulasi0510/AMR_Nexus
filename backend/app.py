import os
from flask import Flask
from flask_cors import CORS

from routes.query import query_bp

app = Flask(__name__)

allowed_origins = [x.strip() for x in os.getenv("AMR_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if x.strip()]
CORS(app, resources={r"/query": {"origins": allowed_origins}})

app.register_blueprint(query_bp)

@app.route("/")
def home():

    return {
        "project": "AMR Agentic AI Retrieval System",
        "status": "Backend Running",
        "version": "2.0-final-scope"
    }

@app.route("/health")
def health():
    return {"status": "ok", "service": "amr-backend"}

if __name__ == "__main__":
    app.run(debug=True)
