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

class KYCForm(forms.Form):
    bvn = forms.CharField(max_length=11, min_length=11, required=False, 
                          widget=forms.TextInput(attrs={'placeholder': 'Enter 11-digit BVN'}))
    nin = forms.CharField(max_length=11, min_length=11, required=False, 
                          widget=forms.TextInput(attrs={'placeholder': 'Enter 11-digit NIN'}))

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get('bvn') and not cleaned_data.get('nin'):
            raise forms.ValidationError("You must provide either a BVN or an NIN.")
        return cleaned_data
