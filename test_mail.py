import mailer

mailer.send_booking_confirmation({
    'booking_id': 1,
    'name': 'Test User',
    'email': 'xyraeunice02@gmail.com',
    'title': 'Test Movie',
    'cinema': 'SM Manila',
    'date': 'October 25, 2026',
    'showtime': '4:00 PM',
    'seats': 'A1, A2',
    'tickets': 2,
    'price': 300.0,
    'total': 600.0,
    'method': 'GCash',
    'paid_at': 'October 24, 2026 02:00 PM',
})
print('Email sent!')