let
  pkgs = import <nixpkgs> { };
in
pkgs.mkShell {
  packages = [
    pkgs.nixfmt-rfc-style
    pkgs.gtk3
    (pkgs.python313.withPackages (ps: [
      (ps.opencv4.override { enableGtk3 = true; })
      ps.numpy
      ps.pillow
    ]))
  ];
}
