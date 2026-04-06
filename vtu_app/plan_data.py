# vtu_app/plan_data.py

NETWORK_CHOICES = [
    ('1', 'MTN'),
    ('2', 'Glo'),
    ('3', 'Etisalat (9mobile)'),
    ('4', 'Airtel'),
]

# Example Plan IDs (You will update these from your ClubKonnect Dashboard 'Price List')
DATA_PLANS = {
    '1': [ # MTN
        ('1001', 'MTN SME 500MB - ₦150'),
        ('1002', 'MTN SME 1GB - ₦250'),
        ('1003', 'MTN SME 2GB - ₦500'),
    ],
    '2': [ # Glo
        ('2001', 'Glo 1.35GB (Monthly) - ₦450'),
    ],
    # Add more networks...
}
