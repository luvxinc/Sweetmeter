"""One-time consent when a downloaded native app is opened directly."""
import sys
from pathlib import Path

from .paths import install_root


def needs_install(args):
    if not getattr(sys, 'frozen', False):
        return False
    if any((args.background, args.headless, args.once, args.preview_only, args.state_dir)):
        return False
    executable = Path(sys.executable).absolute()
    application = executable.parents[2] if sys.platform == 'darwin' else executable.parent
    return application != install_root().absolute()


def welcome():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from .installation import install_current

    root = tk.Tk()
    root.title('Set up Sweetmeter')
    root.geometry('540x300')
    panel = ttk.Frame(root, padding=24)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='Welcome to Sweetmeter', font=('', 20, 'bold')).pack(anchor='w')
    ttk.Label(panel, text='Set up once. Your meter reconnects automatically.',
              wraplength=480).pack(anchor='w', pady=(12, 16))
    ttk.Label(panel, text='This installs Sweetmeter for your user and starts it at login.\n'
              'It uses Bluetooth and your existing Claude Code / Codex sign-ins\n'
              'to display usage. Account credentials stay on this computer.\n\n'
              'Allow Bluetooth when asked, then confirm this computer on the meter.',
              wraplength=480).pack(anchor='w')
    status = tk.StringVar()
    ttk.Label(panel, textvariable=status, wraplength=480).pack(anchor='w', pady=8)

    def install():
        button.configure(state='disabled')
        status.set('Installing and opening Sweetmeter…')
        root.update_idletasks()
        try:
            install_current()
        except Exception as error:
            messagebox.showerror('Setup could not finish', str(error), parent=root)
            button.configure(state='normal')
            status.set('Correct the problem, then try again.')
        else:
            root.destroy()

    button = ttk.Button(panel, text='Install and continue', command=install)
    button.pack(side='right')
    ttk.Button(panel, text='Cancel', command=root.destroy).pack(side='right', padx=8)
    root.mainloop()
