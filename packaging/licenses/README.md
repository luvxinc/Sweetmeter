# Additional runtime notices

The Tcl and Tk `license.terms` files are from the respective upstream
`core-8-6-branch`:

- https://github.com/tcltk/tcl/blob/core-8-6-branch/license.terms
- https://github.com/tcltk/tk/blob/core-8-6-branch/license.terms

The build copies its actual CPython `LICENSE.txt`, dependency wheel notices
(including embedded native-library notices) and exact installed versions into
the app's `NOTICES` directory. Build releases with Python 3.12 / Tcl-Tk 8.6;
changing the runtime requires revisiting its license collection.
