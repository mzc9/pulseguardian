from functools import wraps

from flask import Flask, render_template, session, g, redirect, request, jsonify

from model.base import db_session, init_db
from model.user import User
from model.queue import Queue
from management import PulseManagementAPI, PulseManagementException
from sendemail import sendemail
import config

# Initializing the web app and the database
app = Flask(__name__)
app.secret_key = config.flask_secret_key

# Initializing the rabbitmq management API
pulse_management = PulseManagementAPI()

# Initializing the databse
init_db()

# Decorators and instructions used to inject info into the context or
# restrict access to some pages
def requires_login(f):
    """  Decorator for views that requires the user to be logged-in """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in', None):
            return redirect('/')
        else:
            return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_user():
    """ Injects a user and configuration in templates' context """
    user = User.query.filter(User.email == session.get('logged_in')).first()
    return dict(cur_user=user, config=config)


@app.before_request
def load_user():
    """ Loads the currently logged-in user (if any) to the request context """
    g.user = User.query.filter(User.email == session.get('logged_in')).first()


@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()

# Views


@app.route("/")
def index():
    if g.user:
        return redirect('/profile')
    else:
        return render_template('index.html')


@app.route("/profile")
@requires_login
def profile():
    users = User.query.all()
    return render_template('profile.html', users=users)

# API


@app.route("/queue/<queue_name>", methods=['DELETE'])
def delete_queue(queue_name):

    queue = Queue.query.get(queue_name)
    if queue:
        db_session.delete(queue)
        db_session.commit()
        try:
            pulse_management.delete_queue(vhost='/', queue=queue.name)
            return jsonify(ok=True)
        except PulseManagementException:
            app.logger.warning(
                "Couldn't delete the queue '{}' on rabbitmq".format(queue_name))

    return jsonify(ok=False)


# Login / Signup / Activate user

@app.route("/signup", methods=['POST'])
def signup():
    email, username, password = request.form[
        'email'], request.form['username'], request.form['password']

    # TODO : Add some safeguards as some admin users may exist in rabbitmq but
    # not in our db ?

    if User.query.filter(User.email == email).first():
        return render_template('index.html', signup_error="A user with the same \
                               email already exists")

    if User.query.filter(User.username == username).first():
        return render_template('index.html', signup_error="A user with the same \
                               username already exists")

    user = User.new_user(email=email, username=username, password=password)
    db_session.add(user)
    db_session.commit()

    # Sending the activation email
    activation_link = '{}/activate/{}/{}'.format(
        config.hostname, user.email, user.activation_token)
    sendemail(
        subject="Activate your Pulse account", from_addr=config.email_from, to_addrs=[user.email],
        username=config.email_account, password=config.email_password,
        html_data=render_template('activation_email.html', user=user, activation_link=activation_link))

    return render_template('confirm.html')


@app.route("/activate/<email>/<activation_token>")
def activate(email, activation_token):
    user = User.query.filter(User.email == email).first()

    if user is None:
        return render_template('activate.html', error="No user with the given email")
    elif user.activated:
        return render_template('activate.html', error="Requested user is already activated")
    elif user.activation_token != activation_token:
        return render_template('activate.html', error="Wrong activation token " + user.activation_token)
    else:
        # Activating the user account, which will also create a Pulse account
        # with the same username/password
        user.activate(pulse_management)

        db_session.add(user)
        db_session.commit()

    return render_template('activate.html')


@app.route("/login", methods=['POST'])
def login():
    email, password = request.form['email'], request.form['password']
    user = User.query.filter(User.email == email).first()

    if user is None or not user.valid_password(password):
        return render_template('index.html', email=email, login_error="User doesn't exist or incorrect password")
    elif not user.activated:
        return render_template('index.html', email=email, login_error="User account isn't activated. Please check your emails.")
    else:
        session['logged_in'] = email
        return redirect('/')


@app.route("/logout", methods=['GET'])
def logout():
    del session['logged_in']
    return redirect('/')


if __name__ == "__main__":
    app.run(debug=True)
