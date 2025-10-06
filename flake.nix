{
  description = "A flake for endscopetool";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };
  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python313;
        deps = ps: [
          (ps.opencv4.override { enableGtk3 = true; })
          ps.numpy
          ps.pillow
        ];
        endscopetool = python.pkgs.buildPythonApplication {
          pname = "endscopetool";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          nativeBuildInputs = [
            python.pkgs.setuptools
          ];
          dependencies = deps python.pkgs;
          buildInputs = [ pkgs.gtk3 ];
        };
      in
      {
        packages.default = endscopetool;
        apps.default = {
          type = "app";
          program = "${endscopetool}/bin/endscopetool";
        };
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.nixfmt-rfc-style
            pkgs.gtk3
            (python.withPackages deps)
          ];
        };
      }
    );
}
