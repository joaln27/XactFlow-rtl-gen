// Drives the generated top and checks that the wiring carries a real transaction. Prints PASS only
// if the reply came back through the bus and the parameter override reached the instance.
module tb_soc;
    logic clk_i = 0;
    logic rst_ni = 0;
    logic [7:0] m_result_o;
    logic [15:0] s_data_o;

    soc dut (
        .clk_i(clk_i),
        .rst_ni(rst_ni),
        .m_result_o(m_result_o),
        .s_data_o(s_data_o)
    );

    always #5 clk_i = ~clk_i;

    initial begin
        #12 rst_ni = 1;
        #40;
        if (m_result_o !== 8'h45) begin
            $display("FAIL: result %h, expected 45 (40 + GAIN 5)", m_result_o);
        end else if (s_data_o !== 16'hffff) begin
            $display("FAIL: data_o %h, expected ffff (OUT_W overridden to 16)", s_data_o);
        end else begin
            $display("PASS");
        end
        $finish;
    end
endmodule
