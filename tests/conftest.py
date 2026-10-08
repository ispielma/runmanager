"""No excepthook dialogs and an offscreen Qt, set before the suite imports either."""
import os

os.environ.setdefault('LABSCRIPT_NO_ERROR_DIALOG', '1')
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
