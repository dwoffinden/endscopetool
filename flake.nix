{
  description = "A flake for endscopetool";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
    git-hooks.url = "github:cachix/git-hooks.nix";
  };
  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      git-hooks,
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
          ps.trio
        ];
        python-with-mypy = python.withPackages (
          ps:
          (deps ps)
          ++ [
            ps.mypy
            ps.types-pillow
          ]
        );
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
        pre-commit-check = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            nixfmt-rfc-style.enable = true;
            mypy = {
              enable = true;
              settings = {
                binPath = "${python-with-mypy}/bin/mypy";
              };
            };
            ruff.enable = true;
            ruff-format.enable = true;
          };
        };
      in
      {
        packages.default = endscopetool;
        apps.default = {
          type = "app";
          program = "${endscopetool}/bin/endscopetool";
        };
        checks = {
          inherit pre-commit-check;
        };
        formatter =
          let
            config = self.checks.${system}.pre-commit-check.config;
            script = ''
              ${pkgs.lib.getExe config.package} run --all-files --config ${config.configFile}
            '';
          in
          pkgs.writeShellScriptBin "pre-commit-run" script;
        devShells.default = pkgs.mkShell {
          inherit (pre-commit-check) shellHook;
          buildInputs = pre-commit-check.enabledPackages;
          packages = [
            pkgs.nixfmt-rfc-style
            pkgs.gtk3
            (python.withPackages deps)
          ];
        };
      }
    );
}
