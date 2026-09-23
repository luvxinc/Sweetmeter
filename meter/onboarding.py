"""One-time consent when a downloaded native app is opened directly."""
import sys
from pathlib import Path

from .paths import app_command, install_root, read_install_record


def needs_install(args):
    if not getattr(sys, 'frozen', False):
        return False
    if any((args.background, args.headless, args.once, args.preview_only, args.state_dir)):
        return False
    executable = Path(sys.executable).absolute()
    application = executable.parents[2] if sys.platform == 'darwin' else executable.parent
    return application != install_root().absolute()


def existing_installation():
    """The managed copy elsewhere on this computer, if one is already installed."""
    root = install_root().absolute()
    record = read_install_record()
    if (record.get('kind') == 'native' and record.get('root') == str(root)
            and Path(app_command(root)[0]).is_file()):
        return root
    return None


def welcome():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from .installation import install_current
    existing = existing_installation()

    root = tk.Tk()
    root.title('Set up Sweetmeter')
    root.geometry('540x300')
    panel = ttk.Frame(root, padding=24)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='Welcome to Sweetmeter', font=('', 20, 'bold')).pack(anchor='w')
    ttk.Label(panel, text='Set up once. Your meter reconnects automatically.',
              wraplength=480).pack(anchor='w', pady=(12, 16))
    if existing is not None:
        text = ('Sweetmeter is already installed at\n' + str(existing) + '.\n\n'
                'Continue opens that copy and checks its login startup. This downloaded '
                'copy is not used; you can delete it.')
    else:
        text = ('This installs Sweetmeter for your user and starts it at login.\n'
                'It uses Bluetooth and your existing Claude Code / Codex sign-ins\n'
                'to display usage. Account credentials stay on this computer.\n\n'
                'Allow Bluetooth when asked, then confirm this computer on the meter.')
    ttk.Label(panel, text=text, wraplength=480).pack(anchor='w')
    status = tk.StringVar()
    ttk.Label(panel, textvariable=status, wraplength=480).pack(anchor='w', pady=8)

    def install():
        button.configure(state='disabled')
        status.set('Installing and opening Sweetmeter…')
        root.update_idletasks()
        try:
            install_current()
        except Exception as error:
            detail = str(error) or type(error).__name__
            messagebox.showerror('Setup could not finish', detail, parent=root)
            button.configure(state='normal')
            status.set('Correct the problem, then try again.')
        else:
            root.destroy()

    label = 'Open installed Sweetmeter' if existing is not None else 'Install and continue'
    button = ttk.Button(panel, text=label, command=install)
    button.pack(side='right')
    ttk.Button(panel, text='Cancel', command=root.destroy).pack(side='right', padx=8)
    root.mainloop()
