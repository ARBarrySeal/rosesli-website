import os
from flask import Flask, send_from_directory

app = Flask(__name__, static_folder=".", static_url_path="/static")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/about.html")
@app.route("/about")
def about():
    return send_from_directory(BASE_DIR, "about.html")


@app.route("/programs.html")
@app.route("/programs")
def programs():
    return send_from_directory(BASE_DIR, "programs.html")


@app.route("/corporate.html")
@app.route("/corporate")
def corporate():
    return send_from_directory(BASE_DIR, "corporate.html")


@app.route("/medical.html")
@app.route("/medical")
def medical():
    return send_from_directory(BASE_DIR, "medical.html")


@app.route("/legal.html")
@app.route("/legal")
def legal():
    return send_from_directory(BASE_DIR, "legal.html")


@app.route("/education.html")
@app.route("/education")
def education():
    return send_from_directory(BASE_DIR, "education.html")


@app.route("/work.html")
@app.route("/work")
def work():
    return send_from_directory(BASE_DIR, "work.html")


@app.route("/team.html")
@app.route("/team")
def team():
    return send_from_directory(BASE_DIR, "team.html")


@app.route("/request.html")
@app.route("/request")
def request_page():
    return send_from_directory(BASE_DIR, "request.html")


@app.route("/accessibility-statement.html")
@app.route("/accessibility-statement")
def accessibility():
    return send_from_directory(BASE_DIR, "accessibility-statement.html")


@app.route("/robots.txt")
def robots():
    return send_from_directory(BASE_DIR, "robots.txt", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory(BASE_DIR, "sitemap.xml", mimetype="application/xml")


@app.route("/favicon.svg")
def favicon():
    return send_from_directory(BASE_DIR, "favicon.svg", mimetype="image/svg+xml")


@app.route("/style.css")
def css():
    return send_from_directory(BASE_DIR, "style.css", mimetype="text/css")


@app.route("/script.js")
def js():
    return send_from_directory(BASE_DIR, "script.js", mimetype="application/javascript")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
