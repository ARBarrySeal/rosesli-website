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


@app.route("/specialties.html")
@app.route("/specialties")
def specialties():
    return send_from_directory(BASE_DIR, "specialties.html")


@app.route("/vri.html")
@app.route("/vri")
def vri():
    return send_from_directory(BASE_DIR, "vri.html")


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


@app.route("/site.css")
def css():
    return send_from_directory(BASE_DIR, "site.css", mimetype="text/css")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
