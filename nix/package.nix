{
  lib,
  stdenv,
  cmake,
  obs-studio,
  src,
}:

stdenv.mkDerivation {
  pname = "obs-source-record";
  version = "0.4.8-physarella";
  inherit src;

  nativeBuildInputs = [ cmake ];
  buildInputs = [ obs-studio ];

  cmakeFlags = [ "-DBUILD_OUT_OF_TREE=On" ];

  # The plugin helpers also install a Windows/macOS-style tree; OBS on nix
  # only reads lib/obs-plugins and share/obs/obs-plugins.
  postInstall = ''
    rm -rf $out/{data,obs-plugins}
  '';

  meta = {
    description = "OBS Studio filter that records, streams or replay-buffers a single source";
    homepage = "https://github.com/physarella/obs-source-record";
    license = lib.licenses.gpl2Only;
    platforms = lib.platforms.linux;
  };
}
