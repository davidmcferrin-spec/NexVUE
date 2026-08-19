/**
 * nexvue-portal-whep.js — minimal WHEP offer prep for the portal's watch
 * page: just the multiopus SDP negotiation fix, extracted from the edge's
 * nexvue-vu.js (mungeWhepOfferSdp/prepareWhepOffer, unchanged logic).
 *
 * Why this exists as its own small file instead of copying all of
 * nexvue-vu.js: every NexVUE edge encode publishes 8ch positioned Opus
 * (CLAUDE.md — "Encode always opens DeckLink 8ch and publishes 8ch
 * positioned Opus"). Chrome/Edge won't advertise multiopus in a WHEP offer
 * on their own, and MediaMTX rejects >2ch Opus without it — so without this
 * munge, watch.html's WHEP negotiation can fail outright against a real
 * edge stream, not just lose audio. The full VU meter UI (canvas meters,
 * per-channel solo/mute) is out of scope for this first portal slice — see
 * web-node/nexvue-vu.js if/when the portal grows a real audio meter.
 */
(function (global) {
  "use strict";

  const MULTICHANNEL_OPUS_FMTP = {
    3: "channel_mapping=0,2,1;num_streams=2;coupled_streams=1",
    4: "channel_mapping=0,1,2,3;num_streams=2;coupled_streams=2",
    5: "channel_mapping=0,4,1,2,3;num_streams=3;coupled_streams=2",
    6: "channel_mapping=0,4,1,2,3,5;num_streams=4;coupled_streams=2",
    7: "channel_mapping=0,4,1,2,3,5,6;num_streams=4;coupled_streams=4",
    8: "channel_mapping=0,6,1,4,5,2,3,7;num_streams=5;coupled_streams=4",
  };

  function reservePayloadType(used) {
    for (let i = 30; i <= 127; i++) {
      if ((i <= 63 || i >= 96) && !used.has(String(i))) {
        used.add(String(i));
        return String(i);
      }
    }
    throw new Error("unable to find a free RTP payload type");
  }

  function collectPayloadTypes(sdp) {
    const used = new Set();
    for (const section of sdp.split("m=").slice(1)) {
      const header = section.split("\r\n")[0] || "";
      for (const tok of header.split(" ").slice(3)) {
        if (tok) used.add(tok);
      }
    }
    return used;
  }

  function mungeWhepOfferSdp(sdp) {
    if (typeof sdp !== "string" || !sdp) return sdp;
    if (/multiopus\/48000\//i.test(sdp)) return sdp;
    const sections = sdp.split("m=");
    if (sections.length < 2) return sdp;
    const used = collectPayloadTypes(sdp);
    let edited = false;
    for (let i = 1; i < sections.length; i++) {
      if (!sections[i].startsWith("audio")) continue;
      const lines = sections[i].split("\r\n");
      let insertAt = lines.length;
      while (insertAt > 0 && lines[insertAt - 1] === "") insertAt--;
      for (let ch = 3; ch <= 8; ch++) {
        const pt = reservePayloadType(used);
        lines[0] += ` ${pt}`;
        lines.splice(insertAt, 0, `a=rtpmap:${pt} multiopus/48000/${ch}`);
        insertAt++;
        lines.splice(insertAt, 0, `a=fmtp:${pt} ${MULTICHANNEL_OPUS_FMTP[ch]}`);
        insertAt++;
        lines.splice(insertAt, 0, `a=rtcp-fb:${pt} transport-cc`);
        insertAt++;
      }
      sections[i] = lines.join("\r\n");
      edited = true;
      break;
    }
    return edited ? sections.join("m=") : sdp;
  }

  let multiopusProbe = null;
  function supportsMultiopus() {
    if (multiopusProbe) return multiopusProbe;
    multiopusProbe = new Promise((resolve) => {
      if (typeof RTCPeerConnection === "undefined") {
        resolve(false);
        return;
      }
      const pc = new RTCPeerConnection({ iceServers: [] });
      let pt = "";
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.createOffer()
        .then((offer) => {
          if (/multiopus\/48000\//i.test(offer.sdp || "")) {
            throw new Error("already present");
          }
          const used = collectPayloadTypes(offer.sdp);
          pt = reservePayloadType(used);
          const sections = offer.sdp.split("m=");
          for (let i = 1; i < sections.length; i++) {
            if (!sections[i].startsWith("audio")) continue;
            const lines = sections[i].split("\r\n");
            let insertAt = lines.length;
            while (insertAt > 0 && lines[insertAt - 1] === "") insertAt--;
            lines[0] += ` ${pt}`;
            lines.splice(insertAt, 0, `a=rtpmap:${pt} multiopus/48000/6`);
            lines.splice(insertAt + 1, 0, `a=fmtp:${pt} ${MULTICHANNEL_OPUS_FMTP[6]}`);
            sections[i] = lines.join("\r\n");
            break;
          }
          offer.sdp = sections.join("m=");
          return pc.setLocalDescription(offer);
        })
        .then(() =>
          pc.setRemoteDescription({
            type: "answer",
            sdp:
              "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n" +
              "a=fingerprint:sha-256 " +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:" +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n" +
              `m=audio 9 UDP/TLS/RTP/SAVPF ${pt}\r\n` +
              "c=IN IP4 0.0.0.0\r\na=ice-ufrag:nexvue\r\n" +
              "a=ice-pwd:nexvuemultiopusprobe000000\r\n" +
              "a=fingerprint:sha-256 " +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:" +
              "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n" +
              "a=setup:active\r\na=sendonly\r\na=rtcp-mux\r\n" +
              `a=rtpmap:${pt} multiopus/48000/6\r\na=fmtp:${pt} ${MULTICHANNEL_OPUS_FMTP[6]}\r\n`,
          })
        )
        .then(() => resolve(true))
        .catch(() => resolve(false))
        .finally(() => {
          try {
            pc.close();
          } catch (e) {
            /* ignore */
          }
        });
    });
    return multiopusProbe;
  }

  async function prepareWhepOffer(offer) {
    const base = offer && offer.sdp ? offer.sdp : "";
    const ok = await supportsMultiopus();
    if (!ok) {
      return { sdp: base, multiopus: false };
    }
    const munged = mungeWhepOfferSdp(base);
    return { sdp: munged, multiopus: munged !== base };
  }

  global.NexVuePortalWhep = {
    MULTICHANNEL_OPUS_FMTP,
    mungeWhepOfferSdp,
    supportsMultiopus,
    prepareWhepOffer,
  };
})(typeof window !== "undefined" ? window : globalThis);
