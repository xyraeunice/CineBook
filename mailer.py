import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from io import BytesIO

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import contact_config
import mail_config

# The CineBook logo (white version, made for the red header). If the file is missing,
# the receipts are still sent, just without the logo.
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'static', 'images', 'logo.png')


def get_contact_info():
    """(email, phone) that customers can use to reach the cinema."""
    email = (getattr(contact_config, 'CONTACT_EMAIL', '') or '').strip()
    phone = (getattr(contact_config, 'CONTACT_PHONE', '') or '').strip()
    return email, phone


def _contact_sentence():
    email, phone = get_contact_info()
    bits = [b for b in (email, phone) if b]
    if not bits:
        return "If you think this is a mistake, please contact the cinema."
    return ("If you think this is a mistake, please contact the cinema at "
            + " or ".join(bits) + ".")


def _send(msg):
    """Send a message through Gmail. Tries port 587 first, then port 465.
    Once the message is accepted, errors while closing the connection are ignored."""
    sent = False

    # --- Attempt 1: port 587 (STARTTLS) ---
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=30)
        try:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(mail_config.GMAIL_USERNAME, mail_config.GMAIL_APP_PASSWORD)
            server.send_message(msg)
            sent = True
        finally:
            try:
                server.quit()
            except Exception:
                server.close()
    except Exception as e:
        if sent:
            return
        print('SMTP 587 failed, trying 465:', repr(e))
    else:
        return

    # --- Attempt 2: port 465 (SSL) ---
    server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30)
    try:
        server.login(mail_config.GMAIL_USERNAME, mail_config.GMAIL_APP_PASSWORD)
        server.send_message(msg)
        sent = True
    except Exception:
        if not sent:
            raise
    finally:
        try:
            server.quit()
        except Exception:
            server.close()


# ---------------------------------------------------------------
# PDF RECEIPT
# ---------------------------------------------------------------

def build_receipt_pdf(d):
    """Returns the receipt as PDF bytes."""
    ref = f"{d['booking_id']:06d}"

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    red = HexColor('#e52f45')
    dark = HexColor('#222222')
    grey = HexColor('#777777')
    line_color = HexColor('#dddddd')
    green = HexColor('#198754')

    # Header
    c.setFillColor(red)
    c.rect(0, height - 110, width, 110, fill=1, stroke=0)
    c.setFillColor(white)

    text_x = 50
    try:
        img_w, img_h = ImageReader(LOGO_PATH).getSize()
        logo_h = 68
        logo_w = logo_h * img_w / img_h
        c.drawImage(LOGO_PATH, 50, height - 110 + (110 - logo_h) / 2,
                    width=logo_w, height=logo_h, mask='auto')
        text_x = 50 + logo_w + 14
    except Exception as e:
        print('LOGO ERROR (PDF):', e)

    c.setFont('Helvetica-Bold', 28)
    c.drawString(text_x, height - 62, 'CineBook')
    c.setFont('Helvetica', 11)
    c.drawString(text_x, height - 84, 'PAYMENT RECEIPT')
    c.setFont('Helvetica-Bold', 13)
    c.drawRightString(width - 50, height - 62, f"Receipt #{ref}")
    c.setFont('Helvetica', 10)
    c.drawRightString(width - 50, height - 84, d['paid_at'])

    # PAID badge
    c.setFillColor(green)
    c.roundRect(width - 110, height - 150, 60, 22, 11, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 10)
    c.drawCentredString(width - 80, height - 143, 'PAID')

    y = height - 175

    def section(title):
        nonlocal y
        c.setFillColor(red)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(50, y, title)
        y -= 8
        c.setStrokeColor(line_color)
        c.line(50, y, width - 50, y)
        y -= 20

    def row(label, value):
        nonlocal y
        c.setFillColor(grey)
        c.setFont('Helvetica', 10)
        c.drawString(50, y, label)
        c.setFillColor(dark)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(180, y, str(value)[:70])
        y -= 18

    section('PAYMENT')
    row('Receipt No.', f"#{ref}")
    row('Date Paid', d['paid_at'])
    row('Payment Method', d['method'])
    y -= 10

    section('CUSTOMER')
    row('Name', d['name'])
    row('Email', d['email'])
    y -= 10

    section('BOOKING DETAILS')
    row('Movie', d['title'])
    row('Cinema', d['cinema'])
    row('Date', d['date'])
    row('Showtime', d['showtime'])
    row('Seats', d['seats'])
    y -= 14

    # Items table
    c.setFillColor(HexColor('#f3f3f3'))
    c.rect(50, y - 6, width - 100, 22, fill=1, stroke=0)
    c.setFillColor(grey)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(58, y + 1, 'DESCRIPTION')
    c.drawString(330, y + 1, 'QTY')
    c.drawRightString(440, y + 1, 'PRICE')
    c.drawRightString(width - 58, y + 1, 'AMOUNT')
    y -= 26

    c.setFillColor(dark)
    c.setFont('Helvetica', 10)
    c.drawString(58, y, f"Movie ticket - {d['title']}"[:52])
    c.drawString(335, y, str(d['tickets']))
    c.drawRightString(440, y, f"PHP {d['price']:,.2f}")
    c.drawRightString(width - 58, y, f"PHP {d['total']:,.2f}")
    y -= 14
    c.setStrokeColor(line_color)
    c.line(50, y, width - 50, y)
    y -= 34

    # Total
    c.setFillColor(grey)
    c.setFont('Helvetica', 10)
    c.drawRightString(width - 58, y + 16, 'TOTAL PAID')
    c.setFillColor(dark)
    c.setFont('Helvetica-Bold', 22)
    c.drawRightString(width - 58, y - 8, f"PHP {d['total']:,.2f}")

    # Footer
    c.setStrokeColor(line_color)
    c.setDash(3, 3)
    c.line(50, 120, width - 50, 120)
    c.setDash()
    c.setFillColor(grey)
    c.setFont('Helvetica', 9)
    c.drawCentredString(width / 2, 100, 'Please present this receipt or your Booking ID at the cinema.')
    c.drawCentredString(width / 2, 86, 'Thank you for booking with CineBook!')

    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------
# EMAILS
# ---------------------------------------------------------------

def send_verification_code(to_email, to_name, code):
    msg = EmailMessage()
    msg['Subject'] = 'CineBook Password Reset Verification Code'
    msg['From'] = f"CineBook <{mail_config.GMAIL_USERNAME}>"
    msg['To'] = to_email

    msg.set_content(
        f"Hello {to_name},\n\n"
        f"Your CineBook verification code is: {code}\n\n"
        "This code is valid for 5 minutes. "
        "If you did not request this, you can ignore this email."
    )

    msg.add_alternative(f"""
    <div style="font-family: Arial, sans-serif; background:#111; padding:30px; color:#fff;">
      <div style="max-width:500px; margin:auto; background:#1c1c1c; padding:30px; border-radius:12px;">
        <h2 style="color:#e52f45; text-align:center;">CineBook</h2>
        <p>Hello <strong>{escape(to_name)}</strong>,</p>
        <p>We received a request to reset your CineBook password. Your verification code is:</p>
        <div style="background:#e52f45; color:#fff; font-size:32px; font-weight:bold;
                    letter-spacing:8px; text-align:center; padding:15px; border-radius:8px; margin:25px 0;">
          {escape(code)}
        </div>
        <p>This code is valid for 5 minutes. If you did not request this, you can ignore this email.</p>
      </div>
    </div>
    """, subtype='html')

    _send(msg)


def send_booking_confirmation(d):
    """d is a dict with: booking_id, name, email, title, cinema, date, showtime,
    seats, tickets, price, total, method, paid_at."""
    ref = f"{d['booking_id']:06d}"

    msg = EmailMessage()
    msg['Subject'] = f"CineBook Receipt #{ref} - {d['title']}"
    msg['From'] = f"CineBook <{mail_config.GMAIL_USERNAME}>"
    msg['To'] = d['email']

    msg.set_content(
        "CINEBOOK - PAYMENT RECEIPT\n"
        "==========================\n\n"
        f"Receipt No.:    #{ref}\n"
        f"Date Paid:      {d['paid_at']}\n"
        f"Payment Method: {d['method']}\n"
        "Status:         PAID\n\n"
        f"Customer:       {d['name']}\n"
        f"Email:          {d['email']}\n\n"
        f"Movie:          {d['title']}\n"
        f"Cinema:         {d['cinema']}\n"
        f"Date:           {d['date']}\n"
        f"Showtime:       {d['showtime']}\n"
        f"Seats:          {d['seats']}\n\n"
        f"{d['tickets']} ticket(s) x PHP {d['price']:,.2f} = PHP {d['total']:,.2f}\n"
        f"TOTAL PAID:     PHP {d['total']:,.2f}\n\n"
        "Your PDF receipt is attached to this email.\n"
        "Please present this receipt or your Booking ID at the cinema.\n"
        "Thank you for booking with CineBook!"
    )

    def row(label, value):
        return f"""<tr>
            <td style="padding:6px 0; color:#999; width:120px;">{escape(label)}</td>
            <td style="padding:6px 0; color:#fff; font-weight:bold;">{escape(value)}</td>
        </tr>"""

    info_rows = ''.join([
        row('Receipt No.', f"#{ref}"),
        row('Date Paid', d['paid_at']),
        row('Payment Method', d['method']),
    ])
    customer_rows = ''.join([
        row('Name', d['name']),
        row('Email', d['email']),
    ])
    booking_rows = ''.join([
        row('Movie', d['title']),
        row('Cinema', d['cinema']),
        row('Date', d['date']),
        row('Showtime', d['showtime']),
        row('Seats', d['seats']),
    ])

    # The logo is embedded in the email itself, so it shows even without internet images.
    logo_bytes = None
    logo_cid = make_msgid(domain='cinebook.local')
    try:
        with open(LOGO_PATH, 'rb') as f:
            logo_bytes = f.read()
    except OSError as e:
        print('LOGO ERROR (email):', e)

    logo_cell = (
        f'<td style="width:62px; padding-right:14px; vertical-align:middle;">'
        f'<img src="cid:{logo_cid[1:-1]}" alt="CineBook" width="52" '
        f'style="display:block; border:0;"></td>'
    ) if logo_bytes else ''

    msg.add_alternative(f"""
    <div style="font-family: Arial, sans-serif; background:#111; padding:30px; color:#fff;">
      <div style="max-width:560px; margin:auto; background:#1c1c1c; border-radius:14px; overflow:hidden;">

        <div style="background:#e52f45; padding:18px 28px;">
          <table style="border-collapse:collapse;"><tr>
            {logo_cell}
            <td style="vertical-align:middle;">
              <div style="font-size:12px; letter-spacing:1px; opacity:0.9;">PAYMENT RECEIPT</div>
              <div style="font-size:26px; font-weight:bold;">CineBook</div>
            </td>
          </tr></table>
        </div>

        <div style="padding:28px;">
          <div style="text-align:right; margin-bottom:6px;">
            <span style="background:rgba(25,135,84,0.25); color:#4cd68a; padding:4px 14px;
                         border-radius:50px; font-size:12px; font-weight:bold;">PAID</span>
          </div>

          <table style="width:100%; border-collapse:collapse;">{info_rows}</table>

          <div style="color:#e52f45; font-size:12px; letter-spacing:1px; margin:22px 0 4px;">CUSTOMER</div>
          <table style="width:100%; border-collapse:collapse;">{customer_rows}</table>

          <div style="color:#e52f45; font-size:12px; letter-spacing:1px; margin:22px 0 4px;">BOOKING DETAILS</div>
          <table style="width:100%; border-collapse:collapse;">{booking_rows}</table>

          <table style="width:100%; border-collapse:collapse; margin-top:24px; font-size:14px;">
            <tr style="color:#999; font-size:12px; text-transform:uppercase;">
              <td style="padding:8px 0; border-bottom:1px solid #333;">Description</td>
              <td style="padding:8px 0; border-bottom:1px solid #333; text-align:center;">Qty</td>
              <td style="padding:8px 0; border-bottom:1px solid #333; text-align:right;">Price</td>
              <td style="padding:8px 0; border-bottom:1px solid #333; text-align:right;">Amount</td>
            </tr>
            <tr>
              <td style="padding:12px 0;">Movie ticket &ndash; {escape(d['title'])}</td>
              <td style="padding:12px 0; text-align:center;">{d['tickets']}</td>
              <td style="padding:12px 0; text-align:right;">&#8369;{d['price']:,.2f}</td>
              <td style="padding:12px 0; text-align:right;">&#8369;{d['total']:,.2f}</td>
            </tr>
          </table>

          <div style="border-top:2px dashed #333; margin-top:8px; padding-top:16px; text-align:right;">
            <span style="color:#999;">TOTAL PAID</span><br>
            <span style="font-size:28px; font-weight:bold;">&#8369;{d['total']:,.2f}</span>
          </div>

          <p style="color:#999; font-size:13px; margin-top:24px;">
            Your PDF receipt is attached to this email.
            Please present this receipt or your Booking ID at the cinema.
            Thank you for booking with CineBook!
          </p>
        </div>
      </div>
    </div>
    """, subtype='html')

    if logo_bytes:
        msg.get_body(preferencelist=('html',)).add_related(
            logo_bytes, maintype='image', subtype='png', cid=logo_cid)

    # Attach the PDF receipt. If the PDF fails, the email is still sent without it.
    try:
        pdf_bytes = build_receipt_pdf(d)
        msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf',
                           filename=f"CineBook-Receipt-{ref}.pdf")
    except Exception as e:
        print('PDF RECEIPT ERROR:', e)

    _send(msg)


def send_welcome_email(to_email, to_name):
    msg = EmailMessage()
    msg['Subject'] = 'Welcome to CineBook!'
    msg['From'] = f"CineBook <{mail_config.GMAIL_USERNAME}>"
    msg['To'] = to_email

    msg.set_content(
        f"Hello {to_name},\n\n"
        "Welcome to CineBook! Your account has been created successfully.\n\n"
        "Here is what you can do:\n"
        "  - Browse the movies that are now showing\n"
        "  - Choose your own seats\n"
        "  - Get your receipt by email right after you pay\n"
        "  - View all your bookings in My Bookings\n\n"
        "Enjoy the show!\n"
        "The CineBook Team"
    )

    msg.add_alternative(f"""
    <div style="font-family: Arial, sans-serif; background:#111; padding:30px; color:#fff;">
      <div style="max-width:540px; margin:auto; background:#1c1c1c; border-radius:14px; overflow:hidden;">

        <div style="background:#e52f45; padding:30px 28px; text-align:center;">
          <div style="font-size:13px; letter-spacing:1px; opacity:0.9;">YOUR MOVIE, YOUR SEAT</div>
          <div style="font-size:30px; font-weight:bold; margin-top:6px;">Welcome to CineBook!</div>
        </div>

        <div style="padding:30px;">
          <p style="font-size:16px;">Hello <strong>{escape(to_name)}</strong>,</p>
          <p style="color:#ccc; line-height:1.6;">
            Thank you for joining CineBook. Your account has been created successfully,
            and you can now book your favorite movies online.
          </p>

          <div style="color:#e52f45; font-size:12px; letter-spacing:1px; margin:24px 0 8px;">WHAT YOU CAN DO</div>
          <table style="width:100%; border-collapse:collapse; color:#ddd; font-size:14px;">
            <tr><td style="padding:8px 0;">&#127916;&nbsp; Browse the movies that are now showing</td></tr>
            <tr><td style="padding:8px 0;">&#128186;&nbsp; Choose your own seats</td></tr>
            <tr><td style="padding:8px 0;">&#129534;&nbsp; Get your receipt by email right after you pay</td></tr>
            <tr><td style="padding:8px 0;">&#127915;&nbsp; View all your bookings in My Bookings</td></tr>
          </table>

          <p style="color:#999; font-size:13px; margin-top:28px; border-top:1px solid #333; padding-top:18px;">
            If you did not create this account, you can ignore this email.<br>
            Enjoy the show!<br>
            <strong style="color:#fff;">The CineBook Team</strong>
          </p>
        </div>
      </div>
    </div>
    """, subtype='html')

    _send(msg)


# ---------------------------------------------------------------
# ACCOUNT SECURITY NOTICES (email or password was changed)
# ---------------------------------------------------------------

def _send_account_notice(to_email, to_name, subject, heading, lines, warning,
                         label='ACCOUNT SECURITY'):
    """One security email in the CineBook look. `lines` are plain-text sentences."""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"CineBook <{mail_config.GMAIL_USERNAME}>"
    msg['To'] = to_email

    when = datetime.now().strftime('%B %d, %Y %I:%M %p')

    msg.set_content(
        f"Hello {to_name},\n\n"
        + "\n\n".join(lines)
        + f"\n\nWhen: {when}\n\n{warning}\n\nThe CineBook Team"
    )

    logo_bytes = None
    logo_cid = make_msgid(domain='cinebook.local')
    try:
        with open(LOGO_PATH, 'rb') as f:
            logo_bytes = f.read()
    except OSError as e:
        print('LOGO ERROR (notice):', e)

    logo_cell = (
        f'<td style="width:62px; padding-right:14px; vertical-align:middle;">'
        f'<img src="cid:{logo_cid[1:-1]}" alt="CineBook" width="52" '
        f'style="display:block; border:0;"></td>'
    ) if logo_bytes else ''

    paragraphs = "".join(
        f'<p style="color:#ddd; line-height:1.6; margin:0 0 14px;">{escape(line)}</p>'
        for line in lines
    )

    msg.add_alternative(f"""
    <div style="font-family: Arial, sans-serif; background:#111; padding:30px; color:#fff;">
      <div style="max-width:540px; margin:auto; background:#1c1c1c; border-radius:14px; overflow:hidden;">

        <div style="background:#e52f45; padding:18px 28px;">
          <table style="border-collapse:collapse;"><tr>
            {logo_cell}
            <td style="vertical-align:middle;">
              <div style="font-size:12px; letter-spacing:1px; opacity:0.9;">{escape(label)}</div>
              <div style="font-size:24px; font-weight:bold;">{escape(heading)}</div>
            </td>
          </tr></table>
        </div>

        <div style="padding:28px;">
          <p style="font-size:16px; margin-top:0;">Hello <strong>{escape(to_name)}</strong>,</p>
          {paragraphs}
          <p style="color:#999; font-size:13px; margin:0 0 6px;">When: {escape(when)}</p>

          <div style="background:#2a1a1d; border:1px solid #5a2a31; border-radius:10px;
                      padding:14px 16px; margin-top:20px; color:#ffb3bd; font-size:14px; line-height:1.5;">
            {escape(warning)}
          </div>

          <p style="color:#999; font-size:13px; margin-top:26px; border-top:1px solid #333; padding-top:18px;">
            <strong style="color:#fff;">The CineBook Team</strong>
          </p>
        </div>
      </div>
    </div>
    """, subtype='html')

    if logo_bytes:
        msg.get_body(preferencelist=('html',)).add_related(
            logo_bytes, maintype='image', subtype='png', cid=logo_cid)

    _send(msg)


def send_password_changed_notice(to_email, to_name):
    _send_account_notice(
        to_email, to_name,
        subject='Your CineBook password was changed',
        heading='Password changed',
        lines=['The password of your CineBook account was just changed.'],
        warning="Was this not you? Open the CineBook login page, choose \"Forgot Password?\" "
                "to reset your password right away, and contact us.",
    )


def send_email_changed_notice(old_email, new_email, to_name):
    """Tells the OLD address what happened, and confirms to the NEW address."""
    _send_account_notice(
        old_email, to_name,
        subject='Your CineBook email address was changed',
        heading='Email address changed',
        lines=[f'The email address of your CineBook account was changed to {new_email}.',
               'From now on, use the new address to log in. We will no longer send messages to this address.'],
        warning="Was this not you? Contact us right away and reset your password using the new address.",
    )
    _send_account_notice(
        new_email, to_name,
        subject='This email is now linked to your CineBook account',
        heading='Email address updated',
        lines=[f'This email address is now the login and contact email of your CineBook account '
               f'(changed from {old_email}).'],
        warning="Did you not make this change? Please contact us right away.",
    )


def send_booking_status_email(to_email, to_name, booking_id, summary, status, reason, refunded):
    """Tells the customer that the admin rejected or cancelled their booking."""
    ref = f"#{int(booking_id):06d}"

    if status == 'cancelled':
        subject = f'Your CineBook booking {ref} was cancelled'
        heading = 'Booking cancelled'
        lines = [f'Your booking {ref} was cancelled by the cinema.', summary]
        if reason:
            lines.append(f'Reason: {reason}')
        if refunded:
            lines.append('Your payment was marked as refunded.')
        lines.append('Your seats were released.')
    else:
        subject = f'Your CineBook booking {ref} was rejected'
        heading = 'Booking rejected'
        lines = [f'We could not verify the payment of your booking {ref}.', summary]
        if reason:
            lines.append(f'Reason: {reason}')
        lines.append('Your seats were released. You may book again with a valid receipt.')

    _send_account_notice(
        to_email, to_name,
        subject=subject,
        heading=heading,
        lines=lines,
        warning=_contact_sentence(),
        label='BOOKING UPDATE',
    )
