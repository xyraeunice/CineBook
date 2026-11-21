import hmac
import os
import re
import secrets
import threading
import time
from datetime import date, datetime
from functools import wraps
from io import BytesIO

import bcrypt
from flask import (Flask, abort, flash, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from PIL import Image
from werkzeug.utils import secure_filename

from db import get_db
from payment_config import PAYMENT_ACCOUNTS
from mailer import (build_receipt_pdf, send_booking_confirmation,
                    send_verification_code, send_welcome_email)

app = Flask(__name__)
app.secret_key = 'palitan-mo-ito-ng-mahabang-random-text-123'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # max 5 MB per upload

POSTER_FOLDER = os.path.join(app.root_path, 'static', 'posters')
ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'webp'}

CINEMAS = ["SM Manila", "Robinsons Manila", "Ayala Malls Manila Bay"]
# Default showtimes. Used for movies whose showtimes were never edited by the admin.
SHOWTIMES = ["10:00 AM", "1:00 PM", "4:00 PM", "7:00 PM", "10:00 PM"]

# Two kinds of cinema room. "extra" is added to the movie price per ticket.
# Palitan ang rows/cols/extra kung iba ang gusto mong layout o presyo.
CINEMA_TYPES = {
    "Regular": {"label": "Regular Cinema", "rows": list("ABCDEFGHIJ"), "cols": 15,
                "aisles": [5, 10], "extra": 0, "perks": "", "features": []},
    "VIP": {"label": "VIP Cinema Room", "rows": list("ABCD"), "cols": 8,
            "aisles": [4], "extra": 150, "perks": "Free Drinks + Popcorn",
            "features": ["Big Screen", "Premium Leather Chairs"]},
}

# Online payment. Ang account name/number at QR image ay nasa payment_config.py
PAYMENT_METHODS = ["GCash", "Maya"]
SEAT_PATTERN = re.compile(r'^([A-Z])([1-9]\d?)$')
# GCash and Maya name their reference differently, so each has its own rule.
# Palitan lang ang dalawang numerong ito para baguhin ang haba (pareho sa dalawa).
REFERENCE_MIN = 6
REFERENCE_MAX = 13
REFERENCE_RULES = {
    "GCash": {"label": "Reference No.", "numeric": True, "max": REFERENCE_MAX,
              "regex": rf'^[0-9]{{{REFERENCE_MIN},{REFERENCE_MAX}}}$',
              "hint": f"Numbers only, {REFERENCE_MIN} to {REFERENCE_MAX} digits.",
              "error": f"GCash Reference No. must be numbers only, {REFERENCE_MIN} to {REFERENCE_MAX} digits."},
    "Maya": {"label": "Reference ID", "numeric": False, "max": REFERENCE_MAX,
             "regex": rf'^[A-Za-z0-9]{{{REFERENCE_MIN},{REFERENCE_MAX}}}$',
             "hint": f"Letters and numbers only, {REFERENCE_MIN} to {REFERENCE_MAX} characters.",
             "error": f"Maya Reference ID must be letters and numbers only, {REFERENCE_MIN} to {REFERENCE_MAX} characters."},
}
RECEIPT_FOLDER = os.path.join(app.root_path, 'uploads', 'receipts')
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
CODE_LIFETIME = 300  # 5 minutes (in seconds)
MIN_AGE = 18  # booking involves payment, so users must be adults


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------

def normalize_time(value):
    """'19:00' or '7:00 PM' -> '7:00 PM'. Returns None if it is not a valid time."""
    value = (value or '').strip().upper()
    for fmt in ('%H:%M', '%H:%M:%S', '%I:%M %p'):
        try:
            return datetime.strptime(value, fmt).strftime('%I:%M %p').lstrip('0')
        except ValueError:
            continue
    return None


def get_showtimes(movie):
    """The showtimes the admin set for this movie (or the defaults)."""
    saved = [t.strip() for t in (movie.get('showtimes') or '').split(',') if t.strip()]
    return saved or SHOWTIMES


@app.template_filter('time24')
def time24_filter(value):
    """'7:00 PM' -> '19:00' (the format of <input type="time">)."""
    label = normalize_time(value)
    return datetime.strptime(label, '%I:%M %p').strftime('%H:%M') if label else ''


app.jinja_env.globals['get_showtimes'] = get_showtimes


def get_movie(movie_id):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM movies WHERE id = %s", (movie_id,))
    movie = cur.fetchone()
    cur.close()
    conn.close()
    return movie


def get_occupied(cur, movie_id, cinema, cinema_type, booking_date, showtime, lock=False):
    """Seats held by pending OR confirmed bookings. Rejected bookings free their seats."""
    sql = """
        SELECT bs.seat_number
        FROM booking_seats bs
        INNER JOIN bookings b ON b.id = bs.booking_id
        WHERE b.movie_id = %s
          AND b.cinema = %s
          AND b.cinema_type = %s
          AND b.booking_date = %s
          AND b.showtime = %s
          AND b.booking_status IN ('pending', 'confirmed')
    """
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, (movie_id, cinema, cinema_type, booking_date, showtime))
    return [row[0] for row in cur.fetchall()]


def valid_seat(seat, cinema_type):
    """A seat must exist in the room layout of the chosen cinema type."""
    m = SEAT_PATTERN.match(seat)
    if not m:
        return False
    layout = CINEMA_TYPES[cinema_type]
    return m.group(1) in layout['rows'] and 1 <= int(m.group(2)) <= layout['cols']


def read_booking_form(need_seats=False):
    try:
        movie_id = int(request.form.get('movie_id', 0))
        tickets = int(request.form.get('tickets', 0))
    except ValueError:
        return None, "Invalid booking information."

    cinema = request.form.get('cinema', '').strip()
    cinema_type = request.form.get('cinema_type', '').strip()
    booking_date = request.form.get('date', '').strip()
    showtime = request.form.get('showtime', '').strip()

    try:
        chosen_date = date.fromisoformat(booking_date)
    except ValueError:
        return None, "Invalid booking date."

    if (cinema not in CINEMAS or cinema_type not in CINEMA_TYPES
            or not (1 <= tickets <= 6) or chosen_date < date.today()):
        return None, "Invalid booking details."

    movie = get_movie(movie_id)
    if not movie or movie['status'] != 'now_showing':
        return None, "Movie is not available."

    if showtime not in get_showtimes(movie):
        return None, "That showtime is not available for this movie."

    # A showtime that has already started (or passed) can no longer be booked.
    show_time = datetime.strptime(showtime, '%I:%M %p').time()
    if datetime.combine(chosen_date, show_time) <= datetime.now():
        return None, ("Sorry, the " + showtime + " showtime on " + chosen_date.strftime('%B %d, %Y')
                      + " has already started or passed. Please go back and choose a later showtime.")

    seats = []
    if need_seats:
        seats = [s.strip() for s in request.form.get('seat_numbers', '').split(',') if s.strip()]
        if (len(seats) != tickets or len(set(seats)) != len(seats)
                or not all(valid_seat(s, cinema_type) for s in seats)):
            return None, "Invalid seat selection."

    layout = CINEMA_TYPES[cinema_type]
    unit_price = float(movie['price']) + layout['extra']

    data = {
        'movie': movie,
        'cinema': cinema,
        'cinema_type': cinema_type,
        'cinema_label': layout['label'],
        'perks': layout['perks'],
        'features': layout['features'],
        'unit_price': unit_price,
        'booking_date': booking_date,
        'formatted_date': chosen_date.strftime('%B %d, %Y'),
        'showtime': showtime,
        'tickets': tickets,
        'seats': seats,
        'total': unit_price * tickets,
    }
    return data, None


def make_details(row, seats):
    """Receipt details (used by the PDF receipt and the confirmation email)."""
    tickets = int(row['tickets'])
    total = float(row['total_amount'])
    paid = row.get('verified_at') or row.get('created_at')
    paid_at = paid.strftime('%B %d, %Y %I:%M %p') if hasattr(paid, 'strftime') else 'N/A'

    cinema = row['cinema']
    if row.get('cinema_type') == 'VIP':
        cinema += ' (' + CINEMA_TYPES['VIP']['label'] + ')'

    return {
        'booking_id': row['id'],
        'name': f"{row['given_name']} {row['surname']}".strip(),
        'email': row['email'],
        'title': row['title'],
        'cinema': cinema,
        'date': row['booking_date'].strftime('%B %d, %Y'),
        'showtime': row['showtime'],
        'seats': ', '.join(seats),
        'tickets': tickets,
        'price': total / tickets if tickets else total,
        'total': total,
        'method': row.get('payment_method') or 'Paid online',
        'paid_at': paid_at,
        'perks': row.get('free_perks') or '',
    }


def send_approved_email_async(booking_id):
    """Emails the receipt in the background once the admin approves a booking."""
    def worker():
        try:
            conn = get_db()
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT b.*, m.title, u.given_name, u.surname, u.email
                FROM bookings b
                INNER JOIN movies m ON m.id = b.movie_id
                INNER JOIN users u ON u.id = b.user_id
                WHERE b.id = %s
                LIMIT 1
            """, (booking_id,))
            row = cur.fetchone()
            cur.execute(
                "SELECT seat_number FROM booking_seats WHERE booking_id = %s ORDER BY seat_number ASC",
                (booking_id,)
            )
            seats = [r['seat_number'] for r in cur.fetchall()]
            cur.close()
            conn.close()

            if not row:
                return
            details = make_details(row, seats)
            send_booking_confirmation(details)
            print(f"[BOOKING] Receipt email sent to: {details['email']}")
        except Exception as e:
            print('BOOKING EMAIL ERROR:', e)

    threading.Thread(target=worker, daemon=True).start()


def save_receipt(file):
    """Checks the uploaded screenshot is a real image and stores it. Returns (filename, error)."""
    name = file.filename or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in ALLOWED_EXT:
        return None, 'Receipt must be a JPG, PNG or WEBP image.'

    try:
        img = Image.open(file.stream)
        img.verify()
        if img.format not in ('JPEG', 'PNG', 'WEBP'):
            raise ValueError('unsupported image format')
        file.stream.seek(0)
    except Exception:
        return None, 'The uploaded file is not a valid image.'

    os.makedirs(RECEIPT_FOLDER, exist_ok=True)
    filename = f"{secrets.token_hex(16)}.{'jpg' if ext == 'jpeg' else ext}"
    file.save(os.path.join(RECEIPT_FOLDER, filename))
    return filename, None


def remove_receipt(filename):
    if filename:
        try:
            os.remove(os.path.join(RECEIPT_FOLDER, filename))
        except OSError:
            pass


def send_welcome_async(email, name):
    """Sends the welcome email in the background so registration doesn't wait."""
    def worker():
        try:
            send_welcome_email(email, name)
            print(f"[WELCOME] Welcome email sent to: {email}")
        except Exception as e:
            print('WELCOME EMAIL ERROR:', e)

    threading.Thread(target=worker, daemon=True).start()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT is_admin FROM users WHERE id = %s LIMIT 1", (session['user_id'],))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row or not row[0]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.errorhandler(413)
def too_large(e):
    return "The file is too large. Maximum upload size is 5 MB.", 413


@app.errorhandler(403)
def forbidden(e):
    return "You don't have permission to access this page.", 403


# ---------------------------------------------------------------
# PUBLIC PAGES
# ---------------------------------------------------------------

@app.route('/')
def home():
    conn = get_db()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM movies WHERE status = 'now_showing' ORDER BY id ASC")
    now_showing = cur.fetchall()

    cur.execute("SELECT * FROM movies WHERE status = 'coming_soon' ORDER BY id ASC")
    coming_soon = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('index.html', now_showing=now_showing, coming_soon=coming_soon)


@app.route('/movies')
def movies():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM movies ORDER BY status = 'now_showing' DESC, id ASC")
    all_movies = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('movies.html', movies=all_movies)


@app.route('/movie/<int:movie_id>')
def movie_details(movie_id):
    movie = get_movie(movie_id)
    if not movie:
        return redirect(url_for('home'))
    return render_template('movie_details.html', movie=movie)


# ---------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    next_url = request.args.get('next') or request.form.get('next') or url_for('home')
    # Local pages lang ang pinapayagan
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = url_for('home')

    error = ''
    email_value = ''

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        if not email or not password:
            error = 'Please enter your email and password.'
        elif (re.search(r'\s', request.form.get('email', ''))
              or re.search(r'\s', password)):
            error = 'Spaces are not allowed in the email or password.'
        elif len(email) > 30 or len(password) > 30:
            error = 'Email and password must not exceed 30 characters.'
        elif not EMAIL_PATTERN.match(email):
            error = 'Please enter a valid email address.'
        else:
            conn = get_db()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT * FROM users WHERE email = %s LIMIT 1", (email,))
            user = cur.fetchone()
            cur.close()
            conn.close()

            ok = False
            if user:
                # Ang PHP hash ay nagsisimula sa $2y$, ang Python ay $2b$
                hashed = user['password'].replace('$2y$', '$2b$', 1)
                try:
                    ok = bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
                except ValueError:
                    ok = False

            if ok:
                session.clear()
                session['user_id'] = user['id']
                session['email'] = user['email']
                session['given_name'] = user['given_name']
                session['surname'] = user['surname']
                session['is_admin'] = bool(user.get('is_admin', 0))
                return redirect(next_url)

            error = 'Incorrect email or password.'

            # Tama ang email, mali ang password: email lang ang ibabalik sa box
            if user:
                email_value = email

    return render_template('login.html', error=error, next_url=next_url, email=email_value)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


def check_name(value, label):
    if not value:
        return f'{label} is required.'
    if re.search(r'\s', value):
        return f'{label} must not contain spaces.'
    if len(value) < 2:
        return f'{label} must be at least 2 characters.'
    if len(value) > 30:
        return f'{label} must not exceed 30 characters.'
    if not value[0].isalpha() or not all(c.isalpha() or c in ".'-" for c in value):
        return (f'{label} may only contain letters, hyphens, '
                'apostrophes and periods.')
    if re.search(r"[.'-]{2}", value):
        return f'{label} has too many consecutive symbols.'
    return ''


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = ''
    success = ''
    errors = {}
    form = {}

    if request.method == 'POST':
        form = {
            'email': request.form.get('email', '').strip(),
            'given_name': request.form.get('given_name', ''),
            'surname': request.form.get('surname', ''),
            'phone_number': request.form.get('phone_number', '').strip(),
            'date_of_birth': request.form.get('date_of_birth', '').strip(),
        }
        raw_email = request.form.get('email', '')
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        # Names
        msg = check_name(form['given_name'], 'Given name')
        if msg:
            errors['given_name'] = msg
        msg = check_name(form['surname'], 'Surname')
        if msg:
            errors['surname'] = msg

        # Email
        email = form['email']
        if not email:
            errors['email'] = 'Email is required.'
        elif re.search(r'\s', raw_email):
            errors['email'] = 'Email must not contain spaces.'
        elif len(email) > 30:
            errors['email'] = 'Email must not exceed 30 characters.'
        elif not EMAIL_PATTERN.match(email):
            errors['email'] = 'Please enter a valid email address.'

        # Phone
        if not form['phone_number']:
            errors['phone_number'] = 'Contact number is required.'
        elif not re.match(r'^09\d{9}$', form['phone_number']):
            errors['phone_number'] = 'Contact number must be 11 digits and start with 09.'

        # Date of birth
        if not form['date_of_birth']:
            errors['date_of_birth'] = 'Date of birth is required.'
        else:
            try:
                dob = date.fromisoformat(form['date_of_birth'])
                today = date.today()
                age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                if dob >= today:
                    errors['date_of_birth'] = 'Date of birth cannot be today or in the future.'
                elif age < MIN_AGE:
                    errors['date_of_birth'] = f'You must be at least {MIN_AGE} years old to register.'
                elif age > 120:
                    errors['date_of_birth'] = 'Please enter a valid date of birth.'
            except ValueError:
                errors['date_of_birth'] = 'Please enter a valid date of birth.'

        # Password
        if not password:
            errors['password'] = 'Password is required.'
        elif re.search(r'\s', password):
            errors['password'] = 'Password must not contain spaces.'
        elif len(password) < 8:
            errors['password'] = 'Password must be at least 8 characters.'
        elif len(password) > 30:
            errors['password'] = 'Password must not exceed 30 characters.'
        elif not re.search(r'[A-Z]', password):
            errors['password'] = 'Password must contain at least one uppercase letter.'
        elif not re.search(r'[a-z]', password):
            errors['password'] = 'Password must contain at least one lowercase letter.'
        elif not re.search(r'[0-9]', password):
            errors['password'] = 'Password must contain at least one number.'
        elif email and password.lower() == email.lower():
            errors['password'] = 'Password must not be the same as your email.'

        # Confirm password
        if not confirm:
            errors['confirm_password'] = 'Please confirm your password.'
        elif re.search(r'\s', confirm):
            errors['confirm_password'] = 'Password must not contain spaces.'
        elif password != confirm:
            errors['confirm_password'] = 'Passwords do not match.'

        # Save to the database only if everything is valid
        if not errors:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE email = %s LIMIT 1", (email,))

            if cur.fetchone():
                errors['email'] = 'An account with this email already exists.'
            else:
                hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                try:
                    cur.execute("""
                        INSERT INTO users
                            (email, given_name, surname, password, phone_number, date_of_birth)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (email, form['given_name'], form['surname'],
                          hashed, form['phone_number'], form['date_of_birth']))
                    conn.commit()
                    send_welcome_async(
                        email,
                        f"{form['given_name']} {form['surname']}"
                    )
                    success = 'Account created successfully! You can now log in.'
                except Exception:
                    conn.rollback()
                    error = 'Registration failed. Please try again.'

            cur.close()
            conn.close()

        if errors and not error:
            error = 'Please fix the errors below and fill in the form again.'

        # May mali o kulang, o natapos na: i-clear ang lahat ng laman ng form
        form = {}

    return render_template('register.html', error=error, success=success,
                           form=form, errors=errors, today=date.today().isoformat(),
                           min_age=MIN_AGE)


# ---------------------------------------------------------------
# FORGOT PASSWORD
# ---------------------------------------------------------------

# Pansamantalang imbakan ng reset codes sa server (nawawala kapag nag-restart ang server)
RESETS = {}


def current_reset():
    token = session.get('reset_token')
    entry = RESETS.get(token) if token else None
    if entry and time.time() > entry['expires']:
        RESETS.pop(token, None)
        session.pop('reset_token', None)
        return None
    return entry


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    error = ''
    email_value = ''

    if request.method == 'POST':
        email = request.form.get('email', '').strip()

        if not email:
            error = 'Please enter your email address.'
        elif re.search(r'\s', request.form.get('email', '')):
            error = 'Email must not contain spaces.'
        elif len(email) > 30:
            error = 'Email must not exceed 30 characters.'
        elif not EMAIL_PATTERN.match(email):
            error = 'Please enter a valid email address.'
        else:
            conn = get_db()
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, email, given_name, surname FROM users WHERE email = %s LIMIT 1",
                (email,)
            )
            user = cur.fetchone()
            cur.close()
            conn.close()

            if not user:
                print(f"[RESET] No account found for: {email}")
                error = 'No account was found with that email address.'
            else:
                code = f"{secrets.randbelow(1000000):06d}"
                token = secrets.token_urlsafe(16)
                RESETS[token] = {
                    'email': user['email'],
                    'user_id': user['id'],
                    'code': code,
                    'expires': time.time() + CODE_LIFETIME,
                    'attempts': 0,
                    'verified': False,
                }

                try:
                    send_verification_code(
                        user['email'], f"{user['given_name']} {user['surname']}", code
                    )
                    print(f"[RESET] Verification code sent to: {user['email']}")
                    session['reset_token'] = token
                    return redirect(url_for('verify_code'))
                except Exception as e:
                    print('EMAIL ERROR:', e)
                    RESETS.pop(token, None)
                    error = "We couldn't send the email. Please try again later."
                    # Tama naman ang email, kaya ibabalik ito sa box
                    email_value = email

    return render_template('forgot_password.html', error=error, email=email_value)


@app.route('/verify-code', methods=['GET', 'POST'])
def verify_code():
    entry = current_reset()
    if not entry:
        flash('Your code has expired. Please request a new one.')
        return redirect(url_for('forgot_password'))

    error = ''

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        entry['attempts'] += 1

        if entry['attempts'] > 5:
            RESETS.pop(session.pop('reset_token', None), None)
            flash('Too many wrong attempts. Please request a new code.')
            return redirect(url_for('forgot_password'))

        if not re.match(r'^[0-9]{6}$', code):
            error = 'Verification code must be exactly 6 digits.'
        elif entry['user_id'] is None or not hmac.compare_digest(code, entry['code']):
            error = 'Invalid verification code.'
        else:
            entry['verified'] = True
            return redirect(url_for('reset_password'))

    # Natitirang segundo para sa countdown sa pahina
    seconds_left = max(0, int(entry['expires'] - time.time()))

    return render_template('verify_code.html', email=entry['email'],
                           error=error, seconds_left=seconds_left)


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    entry = current_reset()
    if not entry or not entry['verified'] or entry['user_id'] is None:
        return redirect(url_for('forgot_password'))

    error = ''
    success = ''

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        if not password or not confirm:
            error = 'Please fill in both password fields.'
        elif re.search(r'\s', password) or re.search(r'\s', confirm):
            error = 'Password must not contain spaces.'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters.'
        elif len(password) > 30 or len(confirm) > 30:
            error = 'Password must not exceed 30 characters.'
        elif not re.search(r'[A-Z]', password):
            error = 'Password must contain at least one uppercase letter.'
        elif not re.search(r'[a-z]', password):
            error = 'Password must contain at least one lowercase letter.'
        elif not re.search(r'[0-9]', password):
            error = 'Password must contain at least one number.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            conn = get_db()
            cur = conn.cursor()
            try:
                cur.execute("UPDATE users SET password = %s WHERE id = %s LIMIT 1",
                            (hashed, entry['user_id']))
                conn.commit()
                RESETS.pop(session.pop('reset_token', None), None)
                success = 'Your password has been successfully changed.'
            except Exception:
                conn.rollback()
                error = 'Unable to update your password. Please try again.'
            finally:
                cur.close()
                conn.close()

    return render_template('reset_password.html', error=error, success=success)


# ---------------------------------------------------------------
# MY PROFILE (edit information, change password)
# ---------------------------------------------------------------

def check_password_rules(password, email=''):
    """Same password rules as registration. Returns an error text or ''."""
    if not password:
        return 'Password is required.'
    if re.search(r'\s', password):
        return 'Password must not contain spaces.'
    if len(password) < 8:
        return 'Password must be at least 8 characters.'
    if len(password) > 30:
        return 'Password must not exceed 30 characters.'
    if not re.search(r'[A-Z]', password):
        return 'Password must contain at least one uppercase letter.'
    if not re.search(r'[a-z]', password):
        return 'Password must contain at least one lowercase letter.'
    if not re.search(r'[0-9]', password):
        return 'Password must contain at least one number.'
    if email and password.lower() == email.lower():
        return 'Password must not be the same as your email.'
    return ''


def validate_profile(form, raw_email, current_dob):
    """Checks the profile form with the same rules as registration."""
    errors = {}

    msg = check_name(form['given_name'], 'Given name')
    if msg:
        errors['given_name'] = msg
    msg = check_name(form['surname'], 'Surname')
    if msg:
        errors['surname'] = msg

    email = form['email']
    if not email:
        errors['email'] = 'Email is required.'
    elif re.search(r'\s', raw_email):
        errors['email'] = 'Email must not contain spaces.'
    elif len(email) > 30:
        errors['email'] = 'Email must not exceed 30 characters.'
    elif not EMAIL_PATTERN.match(email):
        errors['email'] = 'Please enter a valid email address.'

    if not form['phone_number']:
        errors['phone_number'] = 'Contact number is required.'
    elif not re.match(r'^09\d{9}$', form['phone_number']):
        errors['phone_number'] = 'Contact number must be 11 digits and start with 09.'

    if not form['date_of_birth']:
        errors['date_of_birth'] = 'Date of birth is required.'
    elif form['date_of_birth'] != current_dob:
        # An unchanged date of birth is left alone. A new one must pass the 18+ rule.
        try:
            dob = date.fromisoformat(form['date_of_birth'])
            today = date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if dob >= today:
                errors['date_of_birth'] = 'Date of birth cannot be today or in the future.'
            elif age < MIN_AGE:
                errors['date_of_birth'] = f'You must be at least {MIN_AGE} years old.'
            elif age > 120:
                errors['date_of_birth'] = 'Please enter a valid date of birth.'
        except ValueError:
            errors['date_of_birth'] = 'Please enter a valid date of birth.'

    return errors


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login', next=url_for('profile')))

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, email, given_name, surname, phone_number, date_of_birth, password
        FROM users WHERE id = %s LIMIT 1
    """, (session['user_id'],))
    user = cur.fetchone()

    if not user:
        cur.close()
        conn.close()
        session.clear()
        return redirect(url_for('login'))

    dob_value = user['date_of_birth']
    current_dob = dob_value.isoformat() if hasattr(dob_value, 'isoformat') else str(dob_value or '')

    form = {
        'email': user['email'],
        'given_name': user['given_name'],
        'surname': user['surname'],
        'phone_number': user['phone_number'] or '',
        'date_of_birth': current_dob,
    }
    errors, pw_errors = {}, {}
    success = pw_success = error = ''

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'profile':
            form = {key: request.form.get(key, '').strip() for key in form}
            errors = validate_profile(form, request.form.get('email', ''), current_dob)

            if not errors:
                cur.execute("SELECT id FROM users WHERE email = %s AND id <> %s LIMIT 1",
                            (form['email'], user['id']))
                if cur.fetchall():
                    errors['email'] = 'An account with this email already exists.'

            if not errors:
                try:
                    cur.execute("""
                        UPDATE users
                        SET email = %s, given_name = %s, surname = %s,
                            phone_number = %s, date_of_birth = %s
                        WHERE id = %s
                    """, (form['email'], form['given_name'], form['surname'],
                          form['phone_number'], form['date_of_birth'], user['id']))
                    conn.commit()
                    session['email'] = form['email']
                    session['given_name'] = form['given_name']
                    session['surname'] = form['surname']
                    success = 'Your information was updated.'
                except Exception:
                    conn.rollback()
                    error = 'Could not save your changes. Please try again.'
            else:
                error = 'Please fix the errors below.'

        elif action == 'password':
            current = request.form.get('current_password', '')
            new = request.form.get('new_password', '')
            confirm = request.form.get('confirm_password', '')

            hashed = user['password'].replace('$2y$', '$2b$', 1)
            try:
                current_ok = bool(current) and bcrypt.checkpw(current.encode('utf-8'),
                                                              hashed.encode('utf-8'))
            except ValueError:
                current_ok = False

            if not current_ok:
                pw_errors['current_password'] = 'Your current password is incorrect.'
            else:
                msg = check_password_rules(new, user['email'])
                if msg:
                    pw_errors['new_password'] = msg
                elif new == current:
                    pw_errors['new_password'] = 'The new password must be different from your current one.'
                elif not confirm:
                    pw_errors['confirm_password'] = 'Please confirm your new password.'
                elif new != confirm:
                    pw_errors['confirm_password'] = 'Passwords do not match.'

            if not pw_errors:
                new_hash = bcrypt.hashpw(new.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                try:
                    cur.execute("UPDATE users SET password = %s WHERE id = %s LIMIT 1",
                                (new_hash, user['id']))
                    conn.commit()
                    pw_success = 'Your password was changed.'
                except Exception:
                    conn.rollback()
                    pw_errors['new_password'] = 'Could not change your password. Please try again.'

    cur.close()
    conn.close()

    return render_template('profile.html', form=form, errors=errors, pw_errors=pw_errors,
                           success=success, pw_success=pw_success, error=error,
                           today=date.today().isoformat(), min_age=MIN_AGE)


# ---------------------------------------------------------------
# BOOKING FLOW
# ---------------------------------------------------------------

@app.route('/booking/<int:movie_id>')
def booking(movie_id):
    if 'user_id' not in session:
        return redirect(url_for('login', next=url_for('booking', movie_id=movie_id)))

    movie = get_movie(movie_id)
    if not movie or movie['status'] != 'now_showing':
        return redirect(url_for('home'))

    return render_template(
        'booking.html',
        movie=movie,
        cinemas=CINEMAS,
        cinema_types=CINEMA_TYPES,
        showtimes=get_showtimes(movie),
        today=date.today().isoformat()
    )


@app.route('/seat-selection', methods=['POST'])
def seat_selection():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    data, error = read_booking_form()
    if error:
        return error, 400

    layout = CINEMA_TYPES[data['cinema_type']]

    conn = get_db()
    cur = conn.cursor()
    occupied = get_occupied(cur, data['movie']['id'], data['cinema'], data['cinema_type'],
                            data['booking_date'], data['showtime'])
    cur.close()
    conn.close()

    return render_template(
        'seat_selection.html',
        movie=data['movie'], cinema=data['cinema'],
        cinema_type=data['cinema_type'], cinema_label=data['cinema_label'],
        perks=data['perks'], features=data['features'], unit_price=data['unit_price'],
        booking_date=data['booking_date'], showtime=data['showtime'],
        tickets=data['tickets'], occupied=occupied,
        rows=layout['rows'], cols=layout['cols'], aisles=layout['aisles'],
        formatted_date=data['formatted_date']
    )


@app.route('/payment', methods=['POST'])
def payment():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    data, error = read_booking_form(need_seats=True)
    if error:
        return error, 400

    payment_error = ''
    method = request.form.get('payment_method', '')
    reference = request.form.get('reference_no', '').replace(' ', '').upper()

    if 'confirm_booking' in request.form:
        receipt = request.files.get('receipt')

        if method not in PAYMENT_METHODS:
            payment_error = 'Please select a payment method.'
        elif not re.match(REFERENCE_RULES[method]['regex'], reference):
            payment_error = REFERENCE_RULES[method]['error']
        elif not receipt or not receipt.filename:
            payment_error = 'Please upload a screenshot of your payment receipt.'
        else:
            saved_name, upload_error = save_receipt(receipt)
            if upload_error:
                payment_error = upload_error
            else:
                conn = get_db()
                cur = conn.cursor()
                try:
                    conn.start_transaction()

                    occupied = get_occupied(cur, data['movie']['id'], data['cinema'],
                                            data['cinema_type'], data['booking_date'],
                                            data['showtime'], lock=True)
                    taken = sorted(set(data['seats']) & set(occupied))

                    cur.execute("""
                        SELECT id FROM bookings
                        WHERE payment_method = %s AND reference_no = %s
                          AND booking_status <> 'rejected'
                        LIMIT 1
                    """, (method, reference))
                    duplicate = cur.fetchall()

                    if taken:
                        conn.rollback()
                        payment_error = ("Sorry. These seat(s) are already taken: "
                                         + ", ".join(taken)
                                         + ". Please go back and select another seat.")
                    elif duplicate:
                        conn.rollback()
                        payment_error = 'This reference number was already used on another booking.'
                    else:
                        cur.execute("""
                            INSERT INTO bookings
                                (user_id, movie_id, cinema, cinema_type, booking_date, showtime,
                                 tickets, total_amount, payment_method, reference_no,
                                 receipt_path, free_perks, payment_status, booking_status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    'pending', 'pending')
                        """, (session['user_id'], data['movie']['id'], data['cinema'],
                              data['cinema_type'], data['booking_date'], data['showtime'],
                              data['tickets'], data['total'], method, reference,
                              saved_name, data['perks'] or None))
                        booking_id = cur.lastrowid

                        cur.executemany(
                            "INSERT INTO booking_seats (booking_id, seat_number) VALUES (%s, %s)",
                            [(booking_id, s) for s in data['seats']]
                        )
                        conn.commit()
                        return redirect(url_for('confirmation', booking_id=booking_id))
                except Exception:
                    conn.rollback()
                    payment_error = 'Your booking could not be submitted. Please try again.'
                finally:
                    cur.close()
                    conn.close()

                # we only get here when the booking was NOT saved
                remove_receipt(saved_name)

    return render_template('payment.html', data=data, methods=PAYMENT_METHODS,
                           accounts=PAYMENT_ACCOUNTS, error=payment_error,
                           selected_method=method, reference_no=reference,
                           ref_rules=REFERENCE_RULES)


@app.route('/confirmation/<int:booking_id>')
def confirmation(booking_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT b.id AS booking_id, b.cinema, b.cinema_type, b.booking_date, b.showtime,
               b.tickets, b.total_amount, b.payment_status, b.booking_status,
               b.payment_method, b.reference_no, b.free_perks, b.admin_remarks,
               m.title, m.genre, m.duration, m.poster,
               u.given_name, u.surname, u.email
        FROM bookings b
        INNER JOIN movies m ON b.movie_id = m.id
        INNER JOIN users u ON b.user_id = u.id
        WHERE b.id = %s AND b.user_id = %s
        LIMIT 1
    """, (booking_id, session['user_id']))
    booking_row = cur.fetchone()

    if not booking_row:
        cur.close()
        conn.close()
        return redirect(url_for('home'))

    cur.execute(
        "SELECT seat_number FROM booking_seats WHERE booking_id = %s ORDER BY seat_number ASC",
        (booking_id,)
    )
    seats = [row['seat_number'] for row in cur.fetchall()]
    cur.close()
    conn.close()

    return render_template('confirmation.html', b=booking_row, seats=seats)


@app.route('/my-bookings')
def my_bookings():
    if 'user_id' not in session:
        return redirect(url_for('login', next=url_for('my_bookings')))

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT b.id, b.cinema, b.cinema_type, b.booking_date, b.showtime, b.tickets,
               b.total_amount, b.payment_status, b.booking_status,
               b.free_perks, b.admin_remarks,
               m.title, m.poster,
               GROUP_CONCAT(bs.seat_number ORDER BY bs.seat_number SEPARATOR ', ') AS seats
        FROM bookings b
        INNER JOIN movies m ON m.id = b.movie_id
        LEFT JOIN booking_seats bs ON bs.booking_id = b.id
        WHERE b.user_id = %s
        GROUP BY b.id, b.cinema, b.cinema_type, b.booking_date, b.showtime, b.tickets,
                 b.total_amount, b.payment_status, b.booking_status,
                 b.free_perks, b.admin_remarks,
                 m.title, m.poster
        ORDER BY b.id DESC
    """, (session['user_id'],))
    bookings = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('my_bookings.html', bookings=bookings)


@app.route('/receipt/<int:booking_id>/download')
def download_receipt(booking_id):
    if 'user_id' not in session:
        return redirect(url_for('login',
                                next=url_for('download_receipt', booking_id=booking_id)))

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT b.*, m.title, u.given_name, u.surname, u.email
        FROM bookings b
        INNER JOIN movies m ON m.id = b.movie_id
        INNER JOIN users u ON u.id = b.user_id
        WHERE b.id = %s AND b.user_id = %s
        LIMIT 1
    """, (booking_id, session['user_id']))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        abort(404)

    cur.execute(
        "SELECT seat_number FROM booking_seats WHERE booking_id = %s ORDER BY seat_number ASC",
        (booking_id,)
    )
    seats = [r['seat_number'] for r in cur.fetchall()]
    cur.close()
    conn.close()

    # The PDF receipt is only available after the admin verified the payment.
    if row['booking_status'] != 'confirmed':
        return redirect(url_for('confirmation', booking_id=booking_id))

    pdf_bytes = build_receipt_pdf(make_details(row, seats))
    return send_file(
        BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"CineBook-Receipt-{booking_id:06d}.pdf"
    )


# ---------------------------------------------------------------
# ADMIN
# ---------------------------------------------------------------

def parse_movie_form():
    """Returns (raw, clean, error). Ang raw ay ginagamit para ibalik ang laman ng form."""
    raw = {key: request.form.get(key, '').strip()
           for key in ('title', 'genre', 'description', 'status',
                       'rating', 'duration', 'price')}

    try:
        duration = int(raw['duration'])
        rating = float(raw['rating'])
        price = float(raw['price'])
    except ValueError:
        return raw, None, 'Duration, rating and price must be valid numbers.'

    times = []
    for value in request.form.getlist('showtimes'):
        if not value.strip():
            continue
        label = normalize_time(value)
        if not label:
            return raw, None, 'One of the showtimes is not a valid time.'
        if label not in times:
            times.append(label)
    if not times:
        return raw, None, 'Please add at least one showtime.'
    if len(times) > 12:
        return raw, None, 'A movie can have at most 12 showtimes.'
    times.sort(key=lambda t: datetime.strptime(t, '%I:%M %p'))

    if not raw['title'] or not raw['genre'] or not raw['description']:
        return raw, None, 'Please fill in all fields.'
    if len(raw['title']) > 100 or len(raw['genre']) > 100:
        return raw, None, 'Title and genre must not exceed 100 characters.'
    if len(raw['description']) > 2000:
        return raw, None, 'Description must not exceed 2000 characters.'
    if raw['status'] not in ('now_showing', 'coming_soon'):
        return raw, None, 'Please choose a valid status.'
    if not (1 <= duration <= 600):
        return raw, None, 'Duration must be between 1 and 600 minutes.'
    if not (0 <= rating <= 10):
        return raw, None, 'Rating must be between 0 and 10.'
    if not (0 <= price <= 10000):
        return raw, None, 'Price must be between 0 and 10,000.'

    clean = {
        'title': raw['title'],
        'genre': raw['genre'],
        'description': raw['description'],
        'status': raw['status'],
        'rating': rating,
        'duration': duration,
        'price': price,
        'showtimes': ','.join(times),
    }
    return raw, clean, ''


def submitted_showtimes():
    """What the admin typed in the showtime boxes (to refill the form after an error)."""
    return [v.strip() for v in request.form.getlist('showtimes') if v.strip()] or ['']


def save_poster(file):
    """Returns (filename, error). Walang file = (None, '')."""
    if not file or not file.filename:
        return None, ''

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXT:
        return None, 'Poster must be a JPG, PNG or WEBP image.'

    base = os.path.splitext(secure_filename(file.filename))[0] or 'poster'
    filename = f"{base}-{secrets.token_hex(3)}.{ext}"
    os.makedirs(POSTER_FOLDER, exist_ok=True)
    file.save(os.path.join(POSTER_FOLDER, filename))
    return filename, ''


@app.route('/admin')
@admin_required
def admin_dashboard():
    conn = get_db()
    cur = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(status = 'now_showing'), 0) AS now_showing,
               COALESCE(SUM(status = 'coming_soon'), 0) AS coming_soon
        FROM movies
    """)
    movie_stats = cur.fetchone()

    cur.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(booking_status = 'pending'), 0) AS pending,
               COALESCE(SUM(CASE WHEN payment_status = 'paid'
                                  AND booking_status = 'confirmed'
                                 THEN total_amount END), 0) AS revenue
        FROM bookings
    """)
    booking_stats = cur.fetchone()

    cur.execute("SELECT COUNT(*) AS total FROM users")
    user_stats = cur.fetchone()

    cur.execute("""
        SELECT b.id, b.cinema, b.booking_date, b.showtime, b.total_amount,
               m.title, u.given_name, u.surname
        FROM bookings b
        INNER JOIN movies m ON m.id = b.movie_id
        INNER JOIN users u ON u.id = b.user_id
        ORDER BY b.id DESC
        LIMIT 5
    """)
    recent = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('admin_dashboard.html', movie_stats=movie_stats,
                           booking_stats=booking_stats, user_stats=user_stats,
                           recent=recent)


@app.route('/admin/movies')
@admin_required
def admin_movies():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.*,
               (SELECT COUNT(*) FROM bookings b WHERE b.movie_id = m.id) AS booking_count
        FROM movies m
        ORDER BY m.id DESC
    """)
    all_movies = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('admin_movies.html', movies=all_movies)


@app.route('/admin/movies/add', methods=['GET', 'POST'])
@admin_required
def admin_movie_add():
    error = ''
    movie = {'status': 'now_showing'}
    showtime_rows = SHOWTIMES

    if request.method == 'POST':
        raw, clean, error = parse_movie_form()
        movie = dict(raw)
        showtime_rows = submitted_showtimes()

        if not error:
            poster, error = save_poster(request.files.get('poster'))
            if not error and not poster:
                error = 'Please upload a poster image.'

        if not error:
            conn = get_db()
            cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO movies
                        (title, genre, duration, rating, price, description, poster, status, showtimes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (clean['title'], clean['genre'], clean['duration'], clean['rating'],
                      clean['price'], clean['description'], poster, clean['status'],
                      clean['showtimes']))
                conn.commit()
                flash(f"\"{clean['title']}\" was added.", 'success')
                return redirect(url_for('admin_movies'))
            except Exception:
                conn.rollback()
                error = 'Could not save the movie. Please try again.'
            finally:
                cur.close()
                conn.close()

    return render_template('admin_movie_form.html', mode='add', movie=movie, error=error,
                           showtime_rows=showtime_rows)


@app.route('/admin/movies/<int:movie_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_movie_edit(movie_id):
    existing = get_movie(movie_id)
    if not existing:
        flash('Movie not found.', 'danger')
        return redirect(url_for('admin_movies'))

    error = ''
    movie = existing
    showtime_rows = get_showtimes(existing)

    if request.method == 'POST':
        raw, clean, error = parse_movie_form()
        movie = dict(raw, id=movie_id, poster=existing['poster'])
        showtime_rows = submitted_showtimes()

        poster = existing['poster']
        if not error:
            new_poster, error = save_poster(request.files.get('poster'))
            if new_poster:
                poster = new_poster

        if not error:
            conn = get_db()
            cur = conn.cursor()
            try:
                cur.execute("""
                    UPDATE movies
                    SET title = %s, genre = %s, duration = %s, rating = %s,
                        price = %s, description = %s, poster = %s, status = %s,
                        showtimes = %s
                    WHERE id = %s
                """, (clean['title'], clean['genre'], clean['duration'], clean['rating'],
                      clean['price'], clean['description'], poster, clean['status'],
                      clean['showtimes'], movie_id))
                conn.commit()
                flash(f"\"{clean['title']}\" was updated.", 'success')
                return redirect(url_for('admin_movies'))
            except Exception:
                conn.rollback()
                error = 'Could not update the movie. Please try again.'
            finally:
                cur.close()
                conn.close()

    return render_template('admin_movie_form.html', mode='edit', movie=movie, error=error,
                           showtime_rows=showtime_rows)


@app.route('/admin/movies/<int:movie_id>/delete', methods=['POST'])
@admin_required
def admin_movie_delete(movie_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE movie_id = %s", (movie_id,))
    booking_count = cur.fetchone()[0]

    if booking_count > 0:
        flash(f"This movie has {booking_count} booking(s) and can't be deleted. "
              "Set it to Coming Soon instead.", 'danger')
    else:
        try:
            cur.execute("DELETE FROM movies WHERE id = %s", (movie_id,))
            conn.commit()
            flash('Movie deleted.', 'success')
        except Exception:
            conn.rollback()
            flash('Could not delete the movie.', 'danger')

    cur.close()
    conn.close()
    return redirect(url_for('admin_movies'))


@app.route('/admin/bookings')
@admin_required
def admin_bookings():
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    if status not in ('pending', 'confirmed', 'rejected'):
        status = ''

    conditions = []
    params = []
    if q:
        conditions.append("""(m.title LIKE %s OR u.email LIKE %s
                              OR CONCAT(u.given_name, ' ', u.surname) LIKE %s
                              OR b.reference_no LIKE %s)""")
        like = f"%{q}%"
        params += [like, like, like, like]
    if status:
        conditions.append("b.booking_status = %s")
        params.append(status)

    sql = """
        SELECT b.id, b.cinema, b.cinema_type, b.booking_date, b.showtime, b.tickets,
               b.total_amount, b.payment_status, b.booking_status,
               b.payment_method, b.reference_no, b.receipt_path, b.free_perks,
               b.admin_remarks, b.verified_at,
               m.title, u.given_name, u.surname, u.email,
               GROUP_CONCAT(bs.seat_number ORDER BY bs.seat_number SEPARATOR ', ') AS seats
        FROM bookings b
        INNER JOIN movies m ON m.id = b.movie_id
        INNER JOIN users u ON u.id = b.user_id
        LEFT JOIN booking_seats bs ON bs.booking_id = b.id
    """
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    # pending bookings first, so the admin sees what needs checking
    sql += """
        GROUP BY b.id, b.cinema, b.cinema_type, b.booking_date, b.showtime, b.tickets,
                 b.total_amount, b.payment_status, b.booking_status,
                 b.payment_method, b.reference_no, b.receipt_path, b.free_perks,
                 b.admin_remarks, b.verified_at,
                 m.title, u.given_name, u.surname, u.email
        ORDER BY (b.booking_status = 'pending') DESC, b.id DESC
        LIMIT 200
    """

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, tuple(params))
    all_bookings = cur.fetchall()

    cur.execute("SELECT booking_status, COUNT(*) AS n FROM bookings GROUP BY booking_status")
    counts = {r['booking_status']: r['n'] for r in cur.fetchall()}
    cur.close()
    conn.close()

    return render_template('admin_bookings.html', bookings=all_bookings, q=q,
                           status=status, counts=counts)


@app.route('/admin/bookings/<int:booking_id>/approve', methods=['POST'])
@admin_required
def admin_booking_approve(booking_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE bookings
        SET payment_status = 'paid', booking_status = 'confirmed',
            admin_remarks = NULL, verified_at = NOW(), verified_by = %s
        WHERE id = %s AND booking_status = 'pending'
    """, (session['user_id'], booking_id))
    changed = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if changed:
        send_approved_email_async(booking_id)
        flash(f"Booking #{booking_id:06d} approved. The customer was emailed their receipt.", 'success')
    else:
        flash('That booking is no longer pending.', 'danger')
    return redirect(url_for('admin_bookings', status=request.form.get('status') or None))


@app.route('/admin/bookings/<int:booking_id>/reject', methods=['POST'])
@admin_required
def admin_booking_reject(booking_id):
    remarks = request.form.get('remarks', '').strip()[:255]
    if not remarks:
        remarks = 'The receipt or reference number could not be verified.'

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE bookings
        SET payment_status = 'rejected', booking_status = 'rejected',
            admin_remarks = %s, verified_at = NOW(), verified_by = %s
        WHERE id = %s AND booking_status = 'pending'
    """, (remarks, session['user_id'], booking_id))
    changed = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if changed:
        flash(f"Booking #{booking_id:06d} rejected. Its seats are available again.", 'success')
    else:
        flash('That booking is no longer pending.', 'danger')
    return redirect(url_for('admin_bookings', status=request.form.get('status') or None))


@app.route('/admin/receipt/<int:booking_id>')
@admin_required
def admin_receipt(booking_id):
    """Shows the receipt screenshot the customer uploaded (admins only)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT receipt_path FROM bookings WHERE id = %s LIMIT 1", (booking_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row[0]:
        abort(404)
    return send_from_directory(RECEIPT_FOLDER, row[0])


if __name__ == '__main__':
    app.run(debug=True)