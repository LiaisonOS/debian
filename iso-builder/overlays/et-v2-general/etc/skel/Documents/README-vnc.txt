===========================================================================
 LiaisonOS — VNC remote desktop
===========================================================================

Two commands (already installed, on PATH):

  et-vnc           run on THIS node (the VNC server)
  et-vnc-viewer    run on the VIEWER PC (watch/control from another computer)

x11vnc (the server) ships with LiaisonOS — et-vnc only configures and starts
it; it does NOT autostart. VNC shares the LIVE desktop (display :0), so the
node must be on an X11 session: at login pick "Xfce session"
(NOT "Xfce Session Wayland").

---------------------------------------------------------------------------
 ON THE NODE  (server)  —  et-vnc
---------------------------------------------------------------------------
  et-vnc           Set the password + start the server. The password is taken
                   automatically from your Winlink password in the profile
                   (~/.config/liaisonos/user.json).
  et-vnc --ask     Type a separate VNC password instead of the profile.
  et-vnc --stop    Stop the running server.

  - Password is stored in ~/.vnc/passwd and is SAVED/RESTORED by the LiaisonOS
    config persistence (survives USB reboots and saved configs).
  - After it starts it prints the address, e.g.  192.168.0.132:5900

  NOTE: VNC passwords are limited to 8 characters, so the VNC password is the
  FIRST 8 CHARACTERS of your Winlink password.

---------------------------------------------------------------------------
 ON THE VIEWER PC  (client)  —  et-vnc-viewer
---------------------------------------------------------------------------
  et-vnc-viewer                     Install the viewer, then prompt for the IP.
  et-vnc-viewer 192.168.0.132       Connect to the node on :5900.
  et-vnc-viewer 192.168.0.132:5901  Connect to an explicit port/display.

  (The viewer is installed on first use. Enter the first 8 characters of the
  node's Winlink password when prompted.)

---------------------------------------------------------------------------
 QUICK TROUBLESHOOTING
---------------------------------------------------------------------------
  Viewer won't connect — on the node:
      pgrep -a x11vnc
      ss -tlnp | grep 5900          (expect 0.0.0.0:5900)
      sudo ufw status               (if active:  sudo ufw allow 5900/tcp)
      ip -4 a | grep inet           (confirm the node IP)
    If it's not running, just run  et-vnc  again (it backgrounds itself).
  Black / no desktop:
      You're on Wayland. Log out, pick "Xfce session" (X11), re-run et-vnc.

  Log file on the node:  ~/.vnc/x11vnc.log
===========================================================================
