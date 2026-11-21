import hmac
import os
import re
import secrets
import time
from datetime import date
from functools import wraps

import bcrypt
from flask import (Flask, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename

from db import get_db
from mailer import send_verification_code, send_verification_code

app = Flask(__name__)
app.secret_key = 'palitan-mo-ito-ng-mahabang-random-text-123'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # max 5 MB per upload

POSTER_FOLDER = os.path.join(app.root_path, 'static', 'posters')
ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'webp'}

CINEMAS = ["SM Manila", "Robinsons Manila", "Ayala Malls Manila Bay"]
SHOWTIMES = ["10:00 AM", "1:00 PM", "4:00 PM", "7:00 PM", "10:00 PM"]
ROWS = list("ABCDEFGHIJ")
PAYMENT_METHODS = ["GCash", "Maya", "Credit/Debit Card"]
SEAT_PATTERN = re.compile(r'^[A-J]([1-9]|1[0-5])$')
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------

def get_movie(movie_id):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM movies WHERE id = %s", (movie_id,))
    movie = cur.fetchone()
    cur.close()
    conn.close()
    return movie


def get_occupied(cur, movie_id, cinema, booking_date, showtime, lock=False):
    sql = """
        SELECT bs.seat_number
        FROM booking_seats bs
        INNER JOIN bookings b ON b.id = bs.booking_id
        WHERE b.movie_id = %s
          AND b.cinema = %s
          AND b.booking_date = %s
          AND b.showtime = %s
          AND b.payment_status = 'paid'
          AND b.booking_status = 'confirmed'
    """
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, (movie_id, cinema, booking_date, showtime))
    return [row[0] for row in cur.fetchall()]


def read_booking_form(need_seats=False):
    try:
        movie_id = int(request.form.get('movie_id', 0))
        tickets = int(request.form.get('tickets', 0))
    except ValueError:
        return None, "Invalid booking information."

    cinema = request.form.get('cinema', '').strip()
    booking_date = request.form.get('date', '').strip()
    showtime = request.form.get('showtime', '').strip()

    try:
        chosen_date = date.fromisoformat(booking_date)
    except ValueError:
        return None, "Invalid booking date."

    if (cinema not in CINEMAS or showtime not in SHOWTIMES
            or not (1 <= tickets <= 6) or chosen_date < date.today()):
        return None, "Invalid booking details."

    movie = get_movie(movie_id)
    if not movie or movie['status'] != 'now_showing':
        return None, "Movie is not available."

    seats = []
    if need_seats:
        seats = [s.strip() for s in request.form.get('seat_numbers', '').split(',') if s.strip()]
        if (len(seats) != tickets or len(set(seats)) != len(seats)
                or not all(SEAT_PATTERN.match(s) for s in seats)):
            return None, "Invalid seat selection."

    data = {
        'movie': movie,
        'cinema': cinema,
        'booking_date': booking_date,
        'formatted_date': chosen_date.strftime('%B %d, %Y'),
        'showtime': showtime,
        'tickets': tickets,
        'seats': seats,
        'total': float(movie['price']) * tickets,
    }
    return data, None


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


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = ''
    success = ''
    form = {}

    if request.method == 'POST':
        form = {
            'email': request.form.get('email', '').strip(),
            'given_name': request.form.get('given_name', '').strip(),
            'surname': request.form.get('surname', '').strip(),
            'phone_number': request.form.get('phone_number', '').strip(),
            'date_of_birth': request.form.get('date_of_birth', '').strip(),
        }
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        try:
            dob = date.fromisoformat(form['date_of_birth'])
        except ValueError:
            dob = None

        if not all(form.values()) or not password or not confirm:
            error = 'Please fill in all fields.'
        elif not EMAIL_PATTERN.match(form['email']):
            error = 'Please enter a valid email address.'
        elif len(form['email']) > 30:
            error = 'Email must not exceed 30 characters.'
        elif not re.match(r'^09\d{9}$', form['phone_number']):
            error = 'Contact number must be 11 digits and start with 09.'
        elif dob is None or dob >= date.today():
            error = 'Please enter a valid date of birth.'
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
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE email = %s LIMIT 1", (form['email'],))

            if cur.fetchone():
                error = 'An account with this email already exists.'
            else:
                hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                try:
                    cur.execute("""
                        INSERT INTO users
                            (email, given_name, surname, password, phone_number, date_of_birth)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (form['email'], form['given_name'], form['surname'],
                          hashed, form['phone_number'], form['date_of_birth']))
                    conn.commit()
                    success = 'Account created successfully! You can now log in.'
                    form = {}
                except Exception:
                    conn.rollback()
                    error = 'Registration failed. Please try again.'

            cur.close()
            conn.close()

    return render_template('register.html', error=error, success=success, form=form)


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
                    'expires': time.time() + 600,
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

    return render_template('verify_code.html', email=entry['email'], error=error)


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
        showtimes=SHOWTIMES,
        today=date.today().isoformat()
    )


@app.route('/seat-selection', methods=['POST'])
def seat_selection():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    data, error = read_booking_form()
    if error:
        return error, 400

    conn = get_db()
    cur = conn.cursor()
    occupied = get_occupied(cur, data['movie']['id'], data['cinema'],
                            data['booking_date'], data['showtime'])
    cur.close()
    conn.close()

    return render_template(
        'seat_selection.html',
        movie=data['movie'], cinema=data['cinema'],
        booking_date=data['booking_date'], showtime=data['showtime'],
        tickets=data['tickets'], occupied=occupied, rows=ROWS,
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

    if 'pay_now' in request.form:
        method = request.form.get('payment_method', '')

        if method not in PAYMENT_METHODS:
            payment_error = 'Please select a payment method.'
        else:
            conn = get_db()
            cur = conn.cursor()
            try:
                conn.start_transaction()

                occupied = get_occupied(cur, data['movie']['id'], data['cinema'],
                                        data['booking_date'], data['showtime'], lock=True)
                taken = sorted(set(data['seats']) & set(occupied))

                if taken:
                    conn.rollback()
                    payment_error = ("Sorry. These seat(s) are already occupied: "
                                     + ", ".join(taken)
                                     + ". Please go back and select another seat.")
                else:
                    cur.execute("""
                        INSERT INTO bookings
                            (user_id, movie_id, cinema, booking_date, showtime,
                             tickets, total_amount, payment_status, booking_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'paid', 'confirmed')
                    """, (session['user_id'], data['movie']['id'], data['cinema'],
                          data['booking_date'], data['showtime'],
                          data['tickets'], data['total']))
                    booking_id = cur.lastrowid

                    cur.executemany(
                    "INSERT INTO booking_seats (booking_id, seat_number) VALUES (%s, %s)",
                        [(booking_id, s) for s in data['seats']]
                    )
                    conn.commit()
                    return redirect(url_for('confirmation', booking_id=booking_id))
            except Exception:
                conn.rollback()
                payment_error = 'Payment could not be completed. Please try again.'
            finally:
                cur.close()
                conn.close()

    return render_template('payment.html', data=data, methods=PAYMENT_METHODS,
                           error=payment_error)


@app.route('/confirmation/<int:booking_id>')
def confirmation(booking_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT b.id AS booking_id, b.cinema, b.booking_date, b.showtime,
               b.tickets, b.total_amount, b.payment_status, b.booking_status,
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
        SELECT b.id, b.cinema, b.booking_date, b.showtime, b.tickets,
               b.total_amount, b.payment_status, b.booking_status,
               m.title, m.poster,
               GROUP_CONCAT(bs.seat_number ORDER BY bs.seat_number SEPARATOR ', ') AS seats
        FROM bookings b
        INNER JOIN movies m ON m.id = b.movie_id
        LEFT JOIN booking_seats bs ON bs.booking_id = b.id
        WHERE b.user_id = %s
        GROUP BY b.id, b.cinema, b.booking_date, b.showtime, b.tickets,
                 b.total_amount, b.payment_status, b.booking_status,
                 m.title, m.poster
        ORDER BY b.id DESC
    """, (session['user_id'],))
    bookings = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('my_bookings.html', bookings=bookings)


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
    }
    return raw, clean, ''


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

    if request.method == 'POST':
        raw, clean, error = parse_movie_form()
        movie = dict(raw)

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
                        (title, genre, duration, rating, price, description, poster, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (clean['title'], clean['genre'], clean['duration'], clean['rating'],
                      clean['price'], clean['description'], poster, clean['status']))
                conn.commit()
                flash(f"\"{clean['title']}\" was added.", 'success')
                return redirect(url_for('admin_movies'))
            except Exception:
                conn.rollback()
                error = 'Could not save the movie. Please try again.'
            finally:
                cur.close()
                conn.close()

    return render_template('admin_movie_form.html', mode='add', movie=movie, error=error)


@app.route('/admin/movies/<int:movie_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_movie_edit(movie_id):
    existing = get_movie(movie_id)
    if not existing:
        flash('Movie not found.', 'danger')
        return redirect(url_for('admin_movies'))

    error = ''
    movie = existing

    if request.method == 'POST':
        raw, clean, error = parse_movie_form()
        movie = dict(raw, id=movie_id, poster=existing['poster'])

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
                        price = %s, description = %s, poster = %s, status = %s
                    WHERE id = %s
                """, (clean['title'], clean['genre'], clean['duration'], clean['rating'],
                      clean['price'], clean['description'], poster, clean['status'],
                      movie_id))
                conn.commit()
                flash(f"\"{clean['title']}\" was updated.", 'success')
                return redirect(url_for('admin_movies'))
            except Exception:
                conn.rollback()
                error = 'Could not update the movie. Please try again.'
            finally:
                cur.close()
                conn.close()

    return render_template('admin_movie_form.html', mode='edit', movie=movie, error=error)


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

    sql = """
        SELECT b.id, b.cinema, b.booking_date, b.showtime, b.tickets,
               b.total_amount, b.payment_status, b.booking_status,
               m.title, u.given_name, u.surname, u.email,
               GROUP_CONCAT(bs.seat_number ORDER BY bs.seat_number SEPARATOR ', ') AS seats
        FROM bookings b
        INNER JOIN movies m ON m.id = b.movie_id
        INNER JOIN users u ON u.id = b.user_id
        LEFT JOIN booking_seats bs ON bs.booking_id = b.id
    """
    params = ()
    if q:
        sql += """
        WHERE m.title LIKE %s OR u.email LIKE %s
           OR CONCAT(u.given_name, ' ', u.surname) LIKE %s
        """
        like = f"%{q}%"
        params = (like, like, like)

    sql += """
        GROUP BY b.id, b.cinema, b.booking_date, b.showtime, b.tickets,
                 b.total_amount, b.payment_status, b.booking_status,
                 m.title, u.given_name, u.surname, u.email
        ORDER BY b.id DESC
        LIMIT 200
    """

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, params)
    all_bookings = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('admin_bookings.html', bookings=all_bookings, q=q)


if __name__ == '__main__':
    app.run(debug=True)