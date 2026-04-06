from django import forms
from .plan_data import NETWORK_CHOICES

class DataPurchaseForm(forms.Form):
    network = forms.ChoiceField(
        choices=[('', 'Select Network')] + NETWORK_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select-lg'})
    )
    plan = forms.ChoiceField(
        choices=[('', 'Select Plan First')],
        widget=forms.Select(attrs={'class': 'form-select-lg'})
    )
    phone_number = forms.CharField(
        max_length=11,
        widget=forms.TextInput(attrs={'placeholder': '08012345678', 'class': 'form-control-lg'})
    )
