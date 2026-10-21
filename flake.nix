{
  description = "Source Record: record, stream or replay-buffer a single OBS source";

  inputs = {
    # The lock on this input is the OBS version the plugin is tested against.
    # Bump it on purpose (nix flake update nixpkgs), then run tests/run.py
    # before anything consumes the new lock.
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";

    # Only for the test harness: nix-built OBS has no GL driver on a non-NixOS
    # host without it.
    nixgl = {
      url = "github:nix-community/nixGL";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { self, nixpkgs, nixgl }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      source-record = pkgs.callPackage ./nix/package.nix { src = self; };
    in
    {
      packages.${system} = {
        default = source-record;
        inherit (pkgs) obs-studio;
      };

      overlays.default = final: prev: {
        obs-studio-plugins = prev.obs-studio-plugins // {
          obs-source-record = final.callPackage ./nix/package.nix { src = self; };
        };
      };

      devShells.${system}.default = pkgs.mkShell {
        inputsFrom = [ source-record ];
        packages = [
          (pkgs.python3.withPackages (p: [ p.obsws-python ]))
          pkgs.xvfb
          pkgs.xdotool
          pkgs.ffmpeg-headless
          nixgl.packages.${system}.nixGLIntel
        ];
        OBS_STUDIO = "${pkgs.obs-studio}";
      };
    };
}
