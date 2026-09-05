#!/bin/bash
#
# Author  : Sylvain Deguire (VA2OPS)
# Purpose : Set Speaker to 100% on the DigiRig Lite.
#           The udev rule at /etc/udev/rules.d/91-et-digirig-lite.rules
#           renames the DigiRig's ALSA card ID to "ET_AUDIO" on plug-in,
#           so we address the card by that stable ID and ignore whatever
#           numeric card index et-audio passes in. Every LiaisonOS-managed
#           audio radio uses the same ET_AUDIO name.
#
#           Only touches Speaker. Mic level stays at whatever the
#           persistent asound.state restored (baseline is 19%).
#

amixer -q -c ET_AUDIO sset Speaker 100% unmute

amixer -q -c ET_AUDIO sset Mic 27% unmute


command -v et-log >/dev/null 2>&1 && \
    et-log "DigiRig Lite: Speaker → 100% on card ET_AUDIO"
